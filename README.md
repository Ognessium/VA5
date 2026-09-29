# VoiceAgentV5 — Full-Duplex Interruptible Voice Agent

> **Theme 05: Interruptible Real-Time Agents** — FDB-v3 Submission

A production-grade, full-duplex voice agent built on LiveKit that handles real-world disfluencies — interruptions, self-corrections, hesitations, and mid-sentence retractions — without ever blocking the conversation or double-booking a tool call.

---

## Architecture Overview

This agent uses a **Dual-Architecture** (Talker + Reasoner) design, separating concerns between sub-second spoken responsiveness and reliable background reasoning.

```
┌─────────────────────────────────────────────────────────────┐
│                        User (voice)                          │
└────────────────────────┬────────────────────────────────────┘
                         │ audio
          ┌──────────────▼──────────────┐
          │   LiveKit Room (WebRTC)      │
          └──────┬───────────────┬───────┘
                 │               │ transcript events
     ┌───────────▼──────┐  ┌────▼───────────────────────────┐
     │   Fast Talker     │  │      Slow Reasoner (Director)   │
     │ Gemini Live V2V   │  │      gemini-3.5-flash-lite (cloud)   │
     │ < 300 ms response │  │      ↳ local qwen2.5 (fallback) │
     │ Reads State API   │◄─│      Reads+Writes State API     │
     └───────────────────┘  └──────────────┬─────────────────┘
                                           │
                         ┌─────────────────▼────────────┐
                         │  Guaranteed Message Queue      │
                         │  (Priority + Dedup + FIFO)    │
                         └─────────────────┬─────────────┘
                                           │
              ┌────────────────────────────▼──────────────────┐
              │                  State API                      │
              │  (versioned, thread-safe, single source of truth)│
              └────────────────────────────┬───────────────────┘
                                           │
                         ┌─────────────────▼────────────┐
                         │       Tool Manager            │
                         │  (Tool Validator → Dispatch)  │
                         └──────────────────────────────┘
```

**Key design principle:** The Talker and Reasoner never block each other. The Talker responds in milliseconds using Gemini Live's native V2V path. The Reasoner works asynchronously in the background, using a priority queue to react to tool completions before new user messages.

See [`docs/architecture.md`](docs/architecture.md) for the full component breakdown.

---

## Quick Setup

### 1. Prerequisites

- Python 3.11+
- A **Google AI Studio** API key (free at [aistudio.google.com](https://aistudio.google.com))
- A **LiveKit Cloud** account (free at [cloud.livekit.io](https://cloud.livekit.io))
- *(Optional)* [Ollama](https://ollama.ai) with `qwen2.5:7b-instruct` pulled, for a local offline fallback

### 2. Clone & Configure

```bash
git clone <your-repo-url>
cd VoiceAgentV5
```

Create your `.env` file in the project root:

```bash
cp .env.example .env
```

Open `.env` and fill in your credentials:

```env
# Google AI Studio API Key
# Used for the Reasoning model (gemini-3.5-flash-lite) AND the V2V Talker (gemini-3.1-flash-live-preview)
# Get your key at: https://aistudio.google.com/apikey
GOOGLE_API_KEY=your_google_api_key_here

# LiveKit Credentials
# Get these from your project dashboard at: https://cloud.livekit.io
# Navigate to: Your Project → Settings → Keys
LIVEKIT_URL=wss://your-project-name.livekit.cloud
LIVEKIT_API_KEY=your_livekit_api_key_here
LIVEKIT_API_SECRET=your_livekit_api_secret_here

# Optional: Talker voice selection
# GOOGLE_VOICE=Puck
```

### 3. Run the Agent

```bash
./start_livekit.sh
```

This script automatically creates a virtual environment, installs all dependencies, and launches the agent. On the very first run it will call the Initializer (an LLM call that reads `tool_manifest.json` and generates the system prompts), then cache the result in `cached_prompts.json` for all subsequent runs.

### 4. Connect & Talk

Open the [LiveKit Agents Playground](https://agents-playground.livekit.io/), enter your LiveKit URL, and connect. Start talking — try interrupting yourself mid-sentence!

---

## Running FDB-v3 Evaluation

### 1. Download Benchmark Data

Follow the [FDB-v3 setup instructions](Full-Duplex-Bench/v3/README.md) to download the benchmark audio files (not included in this repo due to size).

### 2. Start the Agent

```bash
./start_livekit.sh
```

### 3. Run the Evaluator

```bash
cd Full-Duplex-Bench/v3
pip install -r requirements.txt
python evaluate.py \
  --agent-url wss://your-project.livekit.cloud \
  --api-key YOUR_LIVEKIT_KEY \
  --api-secret YOUR_LIVEKIT_SECRET
```

Results (tool-selection F1, argument accuracy, strict pass rate, latency) are saved to `results/`.

---

## Custom Tool Manifest

This agent is **tool-manifest driven** — plug in any domain by editing `tool_manifest.json` without touching agent code:

```json
[
  {
    "name": "your_tool_name",
    "description": "What this tool does.",
    "modifies_state": true,
    "parameters": {
      "param_name": {"type": "string", "description": "..."}
    }
  }
]
```

Tools with `"modifies_state": true` automatically get inverse-tool schemas generated by the Initializer (for mid-conversation undo/cancellation). Delete `cached_prompts.json` after editing the manifest to force a re-initialization.

---

## Extension Use Case

Beyond the FDB-v3 benchmark domains, this agent demonstrates **real-time travel planning with mid-request self-correction**:

1. User: *"Search me a flight to Bangkok for next Friday"* → `search_flights(destination=Bangkok, date=...)` dispatched in background
2. User interrupts: *"Actually wait, make it Bali"* → The Reasoner detects the correction, the in-flight call is cancelled in State, a new `search_flights(destination=Bali, date=...)` call is dispatched
3. Talker bridges the gap naturally: *"Sure, let me switch that to Bali..."* — no dead air, no double-booking, no stale results

This showcases the self-correction and idempotent tool dispatch that are the core of the FDB-v3 challenge.

---

## Models Used

| Component | Primary Model | Fallback |
|-----------|--------------|---------|
| **Talker (V2V)** | `gemini-3.1-flash-live-preview` | — |
| **Reasoner** | `gemini-3.5-flash-lite` | `qwen2.5:7b-instruct` (local Ollama) |
| **Initializer** | `gemma-4-26b-a4b-it` | `qwen2.5:7b-instruct` (local Ollama) |

All primary models are accessed via the Google AI API. No local GPU is required for the primary path. The Ollama fallback activates automatically if the cloud API returns an error.

---

## Project Structure

```
VoiceAgentV5/
├── src/
│   ├── livekit_agent.py        # LiveKit entry point, session setup, event routing
│   ├── system.py               # VoiceAgentSystem orchestrator
│   ├── initializer.py          # Startup LLM calls: manifest ingestion & prompt generation
│   ├── state.py                # Thread-safe, versioned State API
│   ├── tool_manager.py         # Tool dispatch, cancellation, status tracking
│   ├── validator.py            # Tool call format validation (Reasoner → ToolManager)
│   ├── message_queue.py        # Priority queue with exactly-once delivery & dedup
│   ├── manifest_ingestion.py   # Tool manifest parser and inverse-schema generator
│   ├── stt_handler.py          # STT handler (native V2V transcripts used by default)
│   └── agents/
│       ├── reasoner.py         # SlowReasoner: context, LLM calls, tool dispatch
│       └── talker.py           # FastTalker: directive injection into V2V session
├── docs/
│   └── architecture.md         # Detailed component-by-component breakdown
├── Full-Duplex-Bench/          # FDB-v3 evaluation framework (cloned benchmark)
├── tool_manifest.json          # Tool definitions — edit to add your own tools
├── reasoner_instructions.md    # System prompt template for the Reasoner
├── talker_instructions.md      # System prompt template for the Talker
├── cached_prompts.json         # Auto-generated on first run; delete to re-initialize
├── start_livekit.sh            # One-command launcher (creates venv + runs agent)
├── .env.example                # Credentials template (copy to .env)
└── User_testing/app.py         # Real-time Actor Message Tracer dashboard (localhost:8765)
```

---

## Dependencies

Key packages (auto-installed by `start_livekit.sh`):

| Package | Purpose |
|---------|---------|
| `livekit-agents` | LiveKit agent framework |
| `livekit-plugins-google` | Gemini Live (V2V) plugin |
| `livekit-plugins-silero` | VAD (Voice Activity Detection) |
| `google-genai` | Gemini reasoning model SDK |
| `python-dotenv` | `.env` file loading |

---

## Reproducibility Notes

- **No fine-tuning or memorization**: All behavior emerges from the system prompts in `reasoner_instructions.md` and `talker_instructions.md`. The agent has never seen FDB-v3 test items.
- **Model versions are pinned**: Primary model strings are defined in `src/agents/reasoner.py` (`REASONER_MODELS`) and `src/initializer.py`.
- **Deterministic tool outputs**: FDB-v3's benchmark harness provides mocked, deterministic tool responses.
- **API keys**: Required at runtime only. Never hardcoded. See [Quick Setup](#quick-setup) above.

---

## License

See individual component licenses. The FDB-v3 benchmark is subject to its own license at [`Full-Duplex-Bench/LICENSE`](Full-Duplex-Bench/LICENSE).
