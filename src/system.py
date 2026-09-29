import logging
import os
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv

load_dotenv()

from src.state import StateAPI
from src.validator import ToolValidator
from src.tool_manager import ToolManager
from src.manifest_ingestion import ManifestIngestor
from src.agents.reasoner import SlowReasoner
from src.agents.talker import FastTalker
from src.stt_handler import STTHandler
from src.models.messages import DirectorMessage, TalkerTranscript

logger = logging.getLogger(__name__)


class VoiceAgentSystem:
    """
    Top-level orchestrator that owns every component's lifecycle.
    
    The raw tool manifest is provided at runtime during the initialization
    phase (e.g., by the LiveKit session context or external tool API),
    NOT loaded from a static file.
    
    Wiring diagram:
        User Audio ──► STTHandler ──► SlowReasoner (Director)
                                           │
                                    ToolValidator
                                           │
                                      ToolManager ──► MockAPI / External
                                           │
                                        StateAPI ──(direct msg)──► SlowReasoner
                                           │
                                    SlowReasoner ──(DirectorMessage)──► FastTalker
                                           │
                               FastTalker transcript ──(orchestrator)──► SlowReasoner
    """

    def __init__(
        self, 
        raw_manifest: List[Dict[str, Any]], 
        state_id: str = "session_1",
        reasoner_instruction: str = "",
        talker_instruction: str = ""
    ):
        # ── 1. Ingest tool manifest (provided at runtime) ───────────
        ingestor = ManifestIngestor()
        self.schemas = ingestor.ingest(raw_manifest)
        logger.info("Manifest ingested: %s", self.schemas["v2v_tool_summary"])

        # ── 2. Core deterministic state ─────────────────────────────
        self.state_api = StateAPI(state_id)

        # ── 3. Tool pipeline ────────────────────────────────────────
        self.validator = ToolValidator(
            reasoner_schema=self.schemas["reasoner_tool_schema"]
        )
        self.tool_manager = ToolManager(self.state_api)

        # ── 4. Agents ──────────────────────────────────────────────
        self.talker = FastTalker(state_api=self.state_api, system_instruction=talker_instruction)

        self.reasoner = SlowReasoner(
            state_api=self.state_api,
            tool_validator=self.validator,
            tool_manager=self.tool_manager,
            on_direct_callback=self._on_director_message,
            system_instruction=reasoner_instruction
        )

        # ── 5. STT ─────────────────────────────────────────────────
        self.stt_handler = STTHandler()

        # ── Event log (useful for testing / GUI) ────────────────────
        self.event_log: list = []

        logger.info("VoiceAgentSystem initialised (state_id=%s)", state_id)

    # ──────────────────────────────────────────────────────────────
    # Orchestrator-level wiring
    # ──────────────────────────────────────────────────────────────

    def _on_director_message(self, message: DirectorMessage):
        """Reasoner → Talker bridge (one-way)."""
        self.talker.receive_director_message(message)
        self.event_log.append({
            "event": "director_message",
            "type": message.type,
            "content": message.content,
        })
        logger.info("Director → Talker [%s]: %s", message.type, message.content)

    def feed_talker_transcript(self, text: str):
        """
        Orchestrator routes what the Talker said back into the Reasoner.
        (The Talker itself never calls the Reasoner.)
        """
        transcript = TalkerTranscript(content=text)
        self.reasoner.ingest_talker_transcript(transcript)
        self.event_log.append({"event": "talker_transcript", "content": text})

    def feed_user_stt(self, text: str):
        """
        User STT text → Reasoner.

        BUG FIX #9: The previous implementation also spawned a background thread
        to call the text-mode FastTalker (FastTalker.generate_response) and then
        routed its output back through feed_talker_transcript(). In LiveKit mode
        the V2V Gemini Live model IS the Talker — it receives user audio directly
        and responds on its own. Calling the text-mode FastTalker in parallel:
          (a) fires a redundant, unbilled Gemini text call whose reply is never
              delivered to the user (it goes nowhere — LiveKit is not listening),
          (b) pollutes the Reasoner's context with a synthetic "talker" turn that
              did not actually happen, corrupting the conversation history.
        The fix is to route only to the Reasoner here. The LiveKit entrypoint
        handles the Talker path via session events.
        """
        self.reasoner.ingest_user_stt(text)
        self.event_log.append({"event": "user_stt", "content": text})

    def simulate_tool_feedback(self, tool_id: str, status: str, result: Any = None):
        """Simulates an external tool API callback arriving."""
        self.tool_manager.handle_tool_feedback(tool_id, status, result)
        self.event_log.append({
            "event": "tool_feedback",
            "tool_id": tool_id,
            "status": status,
        })

    # ──────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────

    def shutdown(self):
        """Gracefully tears down background threads."""
        self.reasoner.shutdown()
        logger.info("VoiceAgentSystem shut down.")
