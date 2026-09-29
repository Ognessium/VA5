# Architecture — VoiceAgentV5

## Overview

VoiceAgentV5 uses a **Dual-Architecture** design: a **Fast Talker** and a **Slow Reasoner** run concurrently, communicating only through a shared, versioned **State API**. This decoupling is the fundamental reason the agent can be fully responsive to the user while simultaneously executing multi-step tool chains in the background.

```
User (voice)
    │
    ▼
LiveKit Room (WebRTC)
    │
    ├──────────────────────────────────┐
    │ audio stream                     │ conversation_item_added events
    ▼                                  ▼
Fast Talker (V2V)              Slow Reasoner (Director)
gemini-3.1-flash-live-preview  gemini-3.5-flash-lite → qwen2.5 (fallback)
< 300ms spoken response        async, background
Read-only State access    ◄──  Read+Write State access
    ▲                               │
    │  [SYSTEM:SAY/NOTE/INSTRUCTION] │
    └───────────────────────────────┘
                                    │
                     Guaranteed Message Queue
                     (Priority FIFO, Dedup)
                                    │
                          ┌─────────▼──────────┐
                          │     State API        │
                          │  versioned writes    │
                          │  deep-copy reads     │
                          │  thread-safe notify  │
                          └─────────┬────────────┘
                                    │
                          ┌─────────▼──────────┐
                          │    Tool Manager      │
                          │ ┌──────────────────┐│
                          │ │  Tool Validator   ││
                          │ └────────┬─────────┘│
                          │          │           │
                          │   execute / cancel   │
                          └──────────────────────┘
```

---

## Components

### 1. Fast Talker (`src/agents/talker.py`)
- Powered by **Gemini Live (`gemini-3.1-flash-live-preview`)** via a LiveKit `AgentSession`
- Handles all real-time speech I/O — speech synthesis and native STT transcription
- Has **read-only** access to State, injected via the system prompt
- Receives one-way structured directives from the Reasoner:
  - `[SYSTEM:SAY]` — speak this exact content now
  - `[SYSTEM:NOTE]` — background fact to be aware of
  - `[SYSTEM:INSTRUCTION]` — behavioral command (e.g., "ask for the date")
- The Talker acts as a "good actor" — responsive, natural, but not responsible for logic

### 2. Slow Reasoner (`src/agents/reasoner.py`)
- Powered by **`gemini-3.5-flash-lite`** (cloud), with automatic fallback to **`qwen2.5:7b-instruct`** (local Ollama) if the API is unavailable
- Maintains full multi-turn conversation context: user speech, Talker transcripts, tool results, and state updates
- Consumes messages from the **Guaranteed Message Queue** in a dedicated daemon thread — never blocks the LiveKit event loop
- Calls `_analyze_and_direct()` after every meaningful event (user utterance, tool completion) to decide: call a tool, send a directive to the Talker, or output `{}` for no action
- Self-corrects on tool validation failures, up to 2 retries before gracefully giving up

### 3. State API (`src/state.py`)
- Single source of truth for the entire session
- **Versioned**: every write increments a version counter; stale writes are rejected
- **Thread-safe**: reads return deep copies; writes acquire a lock, mutate, then notify without holding the lock
- Tracks: current intent, current focus (10–20 word summary), and all tool calls ever made (in-flight, cancelled, success, failed — history is never deleted)
- Notifies the Reasoner directly via `_on_external_state_change` whenever a non-Reasoner component (e.g., Tool Manager) mutates it

### 4. Tool Manager (`src/tool_manager.py`)
- The only path to external tool execution
- Dispatches tool calls, cancels in-flight ones, tracks status
- All state transitions (tool started, tool succeeded, tool failed) emit a high-priority notification to the Reasoner via the State API's change listener
- Thread-safe: in-flight tools are guarded by a dedicated lock separate from the State lock

### 5. Tool Validator (`src/validator.py`)
- Sits between the Reasoner's LLM output and the Tool Manager
- Validates tool name, parameter types, and required fields against the manifest schema
- On failure, returns the exact error message back to the Reasoner for self-correction
- Prevents malformed calls from ever reaching the Tool Manager

### 6. Guaranteed Message Queue (`src/message_queue.py`)
- Priority queue (high-priority for tool completions, normal for user speech)
- Exactly-once delivery guaranteed via a bounded LRU dedup cache keyed on `msg_id`
- FIFO within the same priority level
- Single consumer daemon thread with graceful shutdown support (`shutdown()`)
- Prevents the Reasoner from triggering twice on `tool_added` events (only reacts to `tool_status_updated` when a tool actually finishes)

### 7. System Initializer (`src/initializer.py`)
- Runs once at startup, before the LiveKit session opens
- Reads `tool_manifest.json` and uses an LLM (`gemma-4-26b-a4b-it`, fallback to local Qwen) to:
  1. Classify tools as `read_only` or `state_modifying`
  2. Generate inverse-tool schemas for all state-modifying tools (for cancellation/undo)
  3. Produce a simplified tool summary for the Talker's system prompt
  4. Produce the full schema + Reasoner instructions prompt
- Results are cached in `cached_prompts.json` — delete this file to force re-initialization after editing the manifest

### 8. LiveKit Agent Entry Point (`src/livekit_agent.py`)
- Implements the `entrypoint(ctx)` coroutine required by the LiveKit agents framework
- Sets up the `AgentSession` with only the V2V `RealtimeModel` — no external STT/VAD override; Gemini Live handles transcription natively
- Listens to `conversation_item_added` events for both user and assistant chat messages
- Routes user utterances → `system.reasoner.ingest_user_stt()`
- Routes Talker transcripts → `system.feed_talker_transcript()`
- Injects Reasoner directives into the session via `session.generate_reply(user_input=...)`

---

## Concurrency Model

| Component | Thread | Mechanism |
|-----------|--------|-----------|
| LiveKit event loop | asyncio main thread | `@session.on(...)` decorators |
| Reasoner consumer | Daemon thread | `GuaranteedMessageQueue` consumer |
| Tool execution | Per-tool daemon thread | `threading.Thread(daemon=True)` |
| Log dashboard pushes | Short-lived daemon thread | Fire-and-forget HTTP POST |
| Directive injection | asyncio main thread | `asyncio.run_coroutine_threadsafe` |

The Reasoner runs entirely off the asyncio event loop. Directives are injected back onto the event loop via `asyncio.run_coroutine_threadsafe`, keeping LiveKit's async session safe.

---

## Interruption & Self-Correction Flow

1. **User speaks**: LiveKit transcribes → `conversation_item_added` fires → `ingest_user_stt()` queues the message
2. **Reasoner wakes**: reads context + State, calls LLM, decides to dispatch a tool
3. **Tool dispatched**: Tool Manager runs it in a background thread, State updated to `in-flight`
4. **User interrupts mid-tool**: New utterance arrives, Reasoner reads the corrected intent from context
5. **Reasoner cancels**: Checks if the tool is still in-flight → if yes, calls `tool_manager.cancel(tool_id)` → State updated to `cancelled`
6. **New tool dispatched**: With corrected arguments, ensuring idempotency (no duplicate state-changing calls)
7. **Tool completes**: State updated to `success`, high-priority notification sent to Reasoner
8. **Reasoner confirms**: Sends `[SYSTEM:SAY]` directive to Talker with the confirmed result
