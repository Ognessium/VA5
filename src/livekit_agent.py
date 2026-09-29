import os
import sys
import logging
import asyncio
import json
import threading
import urllib.request
from datetime import datetime
from dotenv import load_dotenv

# Ensure the root project directory is in the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livekit import agents, rtc
from livekit.agents import AgentSession
from livekit.plugins import google

from src.system import VoiceAgentSystem
from src.initializer import AgentInitializer

load_dotenv()
logging.basicConfig(level=logging.INFO)

with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tool_manifest.json"), "r") as f:
    RUNTIME_MANIFEST = json.load(f)


class MemoryLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.logs = []

    def emit(self, record):
        self.logs.append(self.format(record))
        if len(self.logs) > 100:
            self.logs.pop(0)


memory_handler = MemoryLogHandler()
memory_handler.setFormatter(logging.Formatter('[%(name)s] %(levelname)s: %(message)s'))
logging.getLogger().addHandler(memory_handler)


def log_actor_message(source, destination, content):
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    payload = {
        "timestamp": timestamp,
        "source": source,
        "destination": destination,
        "content": content
    }
    def push():
        try:
            req = urllib.request.Request(
                "http://localhost:8765/api/add_message",
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            urllib.request.urlopen(req, timeout=1.0)
        except Exception:
            pass
    threading.Thread(target=push, daemon=True).start()


async def entrypoint(ctx: agents.JobContext):

    await ctx.connect()
    print(f"✅ Agent connected to LiveKit room: {ctx.room.name}")

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache_file = os.path.join(base_dir, "cached_prompts.json")
    with open(cache_file, "r") as f:
        cached_data = json.load(f)
        r_prompt = cached_data["r_prompt"]
        t_prompt = cached_data["t_prompt"]
        raw_manifest = cached_data["raw_manifest"]

    system = VoiceAgentSystem(raw_manifest=raw_manifest)
    system.reasoner.system_instruction = r_prompt
    talker_prompt = t_prompt

    # Setup Gemini Live (V2V) as the Talker
    model = google.realtime.RealtimeModel(
        model="gemini-3.1-flash-live-preview",
        voice=os.getenv("GOOGLE_VOICE", "Puck"),
        instructions=(
            talker_prompt
            + "\n\nCRITICAL: You MUST always speak in English only, "
            "regardless of what language the user speaks. Never switch to another language."
        ),
    )

    # Initialize LiveKit session with native audio model (it handles STT and VAD automatically)
    session = AgentSession(
        llm=model,
        tools=[]
    )

    loop = asyncio.get_running_loop()

    def on_director_message(message):
        msg_type = str(message.type).upper()
        if msg_type not in ["SAY", "NOTE", "INSTRUCTION"]:
            msg_type = "NOTE"
        msg = f"[SYSTEM:{msg_type}] {message.content}"
        logging.info(f"📥 Injecting into Talker: {msg}")

        async def trigger_reply_async():
            try:
                if hasattr(session, "generate_reply"):
                    # generate_reply is a coroutine — always await it.
                    await session.generate_reply(user_input=msg)
            except Exception as e:
                logging.error(f"generate_reply failed: {e}")

        asyncio.run_coroutine_threadsafe(trigger_reply_async(), loop)
        log_actor_message("Reasoner Engine", "Talker", message.content)

    # Override the default direct callback to route to Gemini Live instead of the mock Talker
    system.reasoner.on_direct_callback = on_director_message

    # BUG FIX #7: Register only the single event that covers both user turns
    # ("user" role) and agent turns ("assistant" role).
    #
    # The previous code stacked four decorators (@session.on("conversation_item_added"),
    # @session.on("user_speech_committed"), @session.on("agent_speech_committed"),
    # @session.on("message")) on the same handler. In LiveKit Agents, each decorator
    # registers an independent listener, so the callback fired once per registered event
    # for a single audio event. "user_speech_committed" and "agent_speech_committed"
    # deliver raw audio/transcript objects with a different shape than the
    # ChatMessage delivered by "conversation_item_added", causing the role/text
    # extraction to silently misfire AND causing every turn to be processed multiple
    # times. Using only "conversation_item_added" is the correct single source of
    # truth for committed chat messages in both directions.
    @session.on("conversation_item_added")
    def on_conversation_item_added(*args, **kwargs):
        try:
            item = None
            if args:
                item = args[0]
            elif "item" in kwargs:
                item = kwargs["item"]

            # Some LiveKit event payloads wrap the message in an .item attribute
            if hasattr(item, "item"):
                item = item.item

            import livekit.agents.llm as llm_module
            text = ""
            role = ""

            if isinstance(item, llm_module.ChatMessage):
                role = item.role
                text = item.content
                if isinstance(text, list):
                    new_text = []
                    for c in text:
                        if isinstance(c, str):
                            new_text.append(c)
                        elif hasattr(c, "text"):
                            new_text.append(c.text)
                    text = " ".join(new_text).strip()
            elif hasattr(item, "role") and hasattr(item, "content"):
                role = item.role
                text = item.content

            if role == "user" and text:
                # Guard: never feed injected system directives back into the Reasoner
                if "[SYSTEM:" in text:
                    return
                logging.info(f"🎙️ V2V STT Received: {text}")
                system.reasoner.ingest_user_stt(text)
                log_actor_message("User", "Talker & Reasoner", text)

            elif role == "assistant" and text:
                logging.info(f"🗣️ V2V Talker Responding: {text}")
                system.feed_talker_transcript(text)
                log_actor_message("Talker", "User & Reasoner", text)

        except Exception as e:
            logging.error(f"Event parsing error: {e}")

    await session.start(
        room=ctx.room,
        agent=agents.Agent(
            instructions=talker_prompt
        ),
    )
    print("🎧 LiveKit V2V Pipeline Started! Speak to Gemini Live...")

    # Wait for audio tracks to fully negotiate before the initial greeting
    await asyncio.sleep(1.5)

    # BUG FIX #6: The Talker's own instructions already tell it to greet the user
    # when the session starts. Injecting a [SYSTEM:INSTRUCTION] directive here was
    # wrong for two reasons:
    #   (a) The Talker handles greetings autonomously per its system prompt — no
    #       external nudge is needed and sending one creates a double-greeting.
    #   (b) The previous code called generate_reply() without awaiting it (used
    #       asyncio.create_task() on what is actually a coroutine), meaning the
    #       coroutine was never scheduled and the greeting was silently dropped.
    # The fix is to simply let the Talker greet on its own. If an explicit initial
    # prompt is ever needed, it must be awaited properly:
    #
    #   await session.generate_reply(
    #       user_input="[SYSTEM:INSTRUCTION] Greet the user warmly and ask how you can help."
    #   )
    #
    # For now we rely on the Talker's instructions (correct per the architecture).


if __name__ == "__main__":
    print("⏳ Pre-generating LLM prompts before starting worker...")
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    manifest_path = os.path.join(base_dir, "tool_manifest.json")
    initializer = AgentInitializer(manifest_path, base_dir)
    r_prompt, t_prompt = initializer.build_prompts()
    cache_file = os.path.join(base_dir, "cached_prompts.json")
    with open(cache_file, "w") as f:
        json.dump({
            "r_prompt": r_prompt,
            "t_prompt": t_prompt,
            "raw_manifest": initializer.manifest
        }, f)
    print("✅ Prompts pre-generated and cached to disk!")

    agents.cli.run_app(
        agents.WorkerOptions(
            agent_name="DualVoiceAgent",
            entrypoint_fnc=entrypoint
        )
    )
