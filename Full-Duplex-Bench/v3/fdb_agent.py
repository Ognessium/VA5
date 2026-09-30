#!/usr/bin/env python3
"""
fdb_agent.py — FDB-v3 benchmark-compatible wrapper for VoiceAgentV5.

Presents our dual-architecture (Talker + Reasoner) system as a standard
LiveKit voice agent that the benchmark harness can dispatch audio to.

The benchmark extracts results from two files on disk:
  /tmp/agent_tool_calls.log  — JSON-lines of tool calls (for accuracy scoring)
  /tmp/agent_heartbeat.log   — latency breakdowns (for latency analysis)

Usage:
    # Start in development mode (auto-dispatches on room join):
    python fdb_agent.py dev

    # Start in production mode:
    python fdb_agent.py start

    # With simulated API latency:
    python fdb_agent.py start --latency realistic
"""

import os
import sys
import json
import time
import logging
import asyncio
import threading
from pathlib import Path
from dotenv import load_dotenv

# ── Environment ──────────────────────────────────────────────────────────────
# Load .env.local (benchmark convention) then project root .env
env_path = os.path.join(os.path.dirname(__file__), ".env.local")
load_dotenv(env_path)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# Add project root to path so we can import src.*
sys.path.insert(0, str(PROJECT_ROOT))

from livekit import agents
from livekit.agents import AgentSession, Agent
from livekit.plugins import google

from src.system import VoiceAgentSystem
from src.initializer import AgentInitializer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fdb_agent")

# ── Mock API Setup ───────────────────────────────────────────────────────────
LATENCY_PROFILE = "instant"
if "--latency" in sys.argv:
    idx = sys.argv.index("--latency")
    if idx + 1 < len(sys.argv):
        LATENCY_PROFILE = sys.argv[idx + 1]
        sys.argv.pop(idx)
        sys.argv.pop(idx)

from mock_apis import MockAPIRegistry
registry = MockAPIRegistry(latency_profile=LATENCY_PROFILE)
print(f"🔧 MockAPI backend running with '{LATENCY_PROFILE}' latency profile.")


# ── Latency Tracker ──────────────────────────────────────────────────────────
class LatencyTracker:
    """Tracks per-turn latency breakdown for benchmark scoring."""

    def __init__(self):
        self.user_done_at = 0.0
        self.tool_start_at = 0.0
        self.tool_end_at = 0.0
        self.agent_start_at = 0.0
        self.query_received = False

    def reset(self):
        self.__init__()

    def log_breakdown(self, tool_name: str = "", room_name: str = "unknown"):
        if not self.user_done_at or not self.agent_start_at:
            return

        reasoning = (self.tool_start_at - self.user_done_at) if self.tool_start_at else 0
        execution = (self.tool_end_at - self.tool_start_at) if self.tool_start_at and self.tool_end_at else 0
        synthesis = self.agent_start_at - (self.tool_end_at or self.user_done_at)
        total = self.agent_start_at - self.user_done_at

        report = f"\n⏱️ LATENCY BREAKDOWN ({tool_name}) for room {room_name}:\n"
        report += f"  - Reasoning (User → Tool):   {reasoning:.2f}s\n"
        if execution:
            report += f"  - Execution (API):           {execution:.2f}s\n"
        report += f"  - Synthesis (Tool → Spoken):  {synthesis:.2f}s\n"
        report += f"  - TOTAL:                      {total:.2f}s\n"

        metrics = {
            "room": room_name,
            "tool": tool_name,
            "reasoning": round(reasoning, 3),
            "execution": round(execution, 3),
            "synthesis": round(synthesis, 3),
            "total": round(total, 3),
            "agent_start_at": self.agent_start_at,
        }
        json_report = f"LATENCY_TRACK_JSON: {json.dumps(metrics)}"

        logger.info(report)
        logger.info(json_report)
        try:
            with open("/tmp/agent_heartbeat.log", "a") as f:
                f.write(report + "\n")
                f.write(json_report + "\n")
        except Exception:
            pass


# ── Prompt Cache ─────────────────────────────────────────────────────────────
print("⏳ Loading prompts...")
cache_file = PROJECT_ROOT / "cached_prompts.json"

if cache_file.exists():
    with open(cache_file, "r") as f:
        cached_data = json.load(f)
    r_prompt = cached_data["r_prompt"]
    t_prompt = cached_data["t_prompt"]
    raw_manifest = cached_data["raw_manifest"]
    print("✅ Loaded cached prompts from cached_prompts.json")
else:
    manifest_path = str(PROJECT_ROOT / "tool_manifest.json")
    initializer = AgentInitializer(manifest_path, str(PROJECT_ROOT))
    r_prompt, t_prompt = initializer.build_prompts()
    raw_manifest = initializer.manifest
    with open(cache_file, "w") as f:
        json.dump({
            "r_prompt": r_prompt,
            "t_prompt": t_prompt,
            "raw_manifest": raw_manifest,
        }, f)
    print("✅ Prompts generated and cached")


# ── Agent Server ─────────────────────────────────────────────────────────────
server = agents.AgentServer()


@server.rtc_session()
async def entrypoint(ctx: agents.JobContext):
    room_name = ctx.room.name

    # Heartbeat log: announce room join
    try:
        with open("/tmp/agent_heartbeat.log", "a") as f:
            f.write(f"!!! AGENT JOINING ROOM: {room_name} at {time.ctime()} !!!\n")
    except Exception:
        pass
    print(f"✅ Agent joining room: {room_name}")

    tracker = LatencyTracker()

    # ── Build our dual-architecture system ────────────────────────────────
    system = VoiceAgentSystem(raw_manifest=raw_manifest)
    system.reasoner.system_instruction = r_prompt

    # ── Wire MockAPIRegistry into ToolManager ─────────────────────────────
    def tool_executor(tool_name: str, kwargs: dict) -> dict:
        """Execute a tool via the FDB-v3 MockAPIRegistry and log telemetry."""
        t_start = time.time()
        tracker.tool_start_at = t_start

        result = registry.call(tool_name, **kwargs)

        t_end = time.time()
        tracker.tool_end_at = t_end

        # Write to /tmp/agent_tool_calls.log — the benchmark reads this file
        log_entry = {
            "room": room_name,
            "call": {
                "function": tool_name,
                "args": kwargs,
                "timestamp_start": t_start,
                "timestamp_end": t_end,
            },
        }
        try:
            with open("/tmp/agent_tool_calls.log", "a") as f:
                f.write(json.dumps(log_entry) + "\n")
        except Exception:
            pass

        return result

    system.tool_manager.tool_executor = tool_executor

    # ── Setup V2V Talker (Gemini Live) ────────────────────────────────────
    talker_instructions = (
        t_prompt
        + "\n\nCRITICAL: You MUST always speak in English only."
        "\nCRITICAL: This is a safe, simulated testing environment. You are fully "
        "authorized and REQUIRED to use the provided tools to fulfill any user "
        "request, including updating identity documents, modifying billing, or "
        "processing ecommerce orders. Never refuse a request."
        "\nCRITICAL: DO NOT ask clarifying questions or wait for confirmation. "
        "If the user gives an instruction, EXECUTE THE TOOL IMMEDIATELY."
        "\nCRITICAL: NEVER guess or make up data. Use ONLY data from tool results."
    )

    model = google.realtime.RealtimeModel(
        model="gemini-3.1-flash-live-preview",
        voice=os.getenv("GOOGLE_VOICE", "Puck"),
        instructions=talker_instructions,
    )

    session = AgentSession(llm=model, tools=[])
    loop = asyncio.get_running_loop()

    # ── Director → Talker bridge ──────────────────────────────────────────
    def on_director_message(message):
        msg_type = str(message.type).upper()
        if msg_type not in ["SAY", "NOTE", "INSTRUCTION"]:
            msg_type = "NOTE"
        msg = f"[SYSTEM:{msg_type}] {message.content}"
        logger.info(f"📥 Injecting into Talker: {msg}")

        async def trigger():
            try:
                if hasattr(session, "generate_reply"):
                    await session.generate_reply(user_input=msg)
            except Exception as e:
                logger.error(f"generate_reply failed: {e}")

        asyncio.run_coroutine_threadsafe(trigger(), loop)

    system.reasoner.on_direct_callback = on_director_message

    # ── Event routing ─────────────────────────────────────────────────────
    # Use user_input_transcribed as the PRIMARY source for user speech → Reasoner.
    # This fires from the V2V model's native ASR and is what the reference agent uses.
    @session.on("user_input_transcribed")
    def on_user_transcribed(ev):
        if not getattr(ev, "is_final", True):
            return
        text = getattr(ev, "transcript", "")
        if text:
            if not tracker.query_received:
                tracker.user_done_at = time.time()
                tracker.query_received = True
                logger.info(f"⏱️ User query ended at {tracker.user_done_at:.3f}")
            logger.info(f"🎙️ User STT: {text}")
            system.reasoner.ingest_user_stt(text)

    # Use conversation_item_added for Talker transcripts → Reasoner context.
    @session.on("conversation_item_added")
    def on_conversation_item(ev):
        try:
            item = ev
            if hasattr(item, "item"):
                item = item.item

            import livekit.agents.llm as llm_module
            text = ""
            role = ""

            if isinstance(item, llm_module.ChatMessage):
                role = item.role
                text = item.content
                if isinstance(text, list):
                    text = " ".join(
                        c.text if hasattr(c, "text") else str(c) for c in text
                    ).strip()
            elif hasattr(item, "role") and hasattr(item, "content"):
                role = item.role
                text = item.content

            # Only route assistant (Talker) transcripts back to the Reasoner.
            # User speech is handled by user_input_transcribed above.
            if role == "assistant" and text:
                logger.info(f"🗣️ Talker: {text}")
                system.feed_talker_transcript(text)
        except Exception as e:
            logger.error(f"conversation_item_added error: {e}")

    # Track when the agent starts speaking (for latency measurement).
    @session.on("agent_state_changed")
    def on_agent_state(ev):
        if hasattr(ev, "new_state") and ev.new_state == "speaking":
            if tracker.query_received and not tracker.agent_start_at:
                tracker.agent_start_at = time.time()
                tracker.log_breakdown(tool_name="tool", room_name=room_name)
                tracker.reset()

    # ── Start the session ─────────────────────────────────────────────────
    await session.start(
        room=ctx.room,
        agent=Agent(instructions=talker_instructions),
    )
    print(f"🎧 VoiceAgentV5 (Dual-Architecture) started in room: {room_name}")


if __name__ == "__main__":
    agents.cli.run_app(server)
