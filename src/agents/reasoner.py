import os
import json
import uuid
import logging
from typing import List, Callable, Dict, Any, Optional
from src.models.messages import TalkerTranscript, DirectorMessage
from src.state import StateAPI
from src.validator import ToolValidator
from src.tool_manager import ToolManager
from src.message_queue import GuaranteedMessageQueue
import urllib.request
import threading
from datetime import datetime
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# Maximum self-correction attempts on validator failure before giving up
_MAX_VALIDATION_RETRIES = 2


def log_reasoner_internal(source, destination, content):
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    payload = {
        "timestamp": timestamp,
        "source": source,
        "destination": destination,
        "content": str(content)
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


class SlowReasoner:
    """
    The Director (Slow Reasoner).
    Maintains full context, analyzes hidden meaning, and directs the Talker.
    Emits tool calls through a Validator before they reach the ToolManager.
    """
    def __init__(
        self, 
        state_api: StateAPI, 
        tool_validator: ToolValidator,
        tool_manager: ToolManager,
        on_direct_callback: Callable[[DirectorMessage], None],
        system_instruction: str = ""
    ):
        self.state_api = state_api
        self.tool_validator = tool_validator
        self.tool_manager = tool_manager
        self.on_direct_callback = on_direct_callback 
        self.system_instruction = system_instruction
        self.context: List[Dict[str, str]] = []
        
        # Exactly-once, prioritized queue for incoming messages
        self.message_queue = GuaranteedMessageQueue(consumer_callback=self._process_message)
        
        # Direct messaging from StateAPI -> Reasoner, routed into our queue
        self.state_api.set_reasoner_callback(self._on_external_state_change)
        
        api_key = os.environ.get("GOOGLE_API_KEY")
        if api_key:
            self.client = genai.Client(api_key=api_key)
        else:
            self.client = None

    def _on_external_state_change(self, change_type: str, data: Any):
        """
        Called by StateAPI when a non-Reasoner component mutates state.
        Routed into the priority queue as high-priority so the Reasoner
        reacts to tool completions before processing new user input.
        """
        msg_id = str(uuid.uuid4())
        payload = {"type": "state_update", "change_type": change_type, "data": data}
        self.message_queue.publish(msg_id, payload, is_high_priority=True)

    def _process_message(self, payload: Any):
        """Queue consumer. Guarantees exactly-once processing in background thread."""
        msg_type = payload.get("type")
        
        if msg_type == "state_update":
            # BUG FIX #2: role must be "user" not "system" — multi-turn APIs (Ollama/Gemini)
            # only allow a single "system" entry at position 0. Subsequent system-role
            # messages are either rejected or silently dropped, so state notifications
            # were never reaching the LLM. Using "user" with a clear prefix keeps the
            # information visible in the conversation window.
            self.context.append({
                "role": "user",
                "content": f"[STATE UPDATE — {payload['change_type']}]: {payload['data']}"
            })
            
            # Prevent the Reasoner from analyzing and generating again just because a tool started.
            # We only want it to react when a tool finishes or state actually changes.
            if payload['change_type'] != "tool_added":
                self._analyze_and_direct()
        elif msg_type == "user_stt":
            # Context already appended synchronously in ingest_user_stt for UI responsiveness
            self._analyze_and_direct()

    def ingest_talker_transcript(self, transcript: TalkerTranscript):
        """Receives what the Talker said (routed by the orchestrator, not by the Talker)."""
        self.context.append({
            "role": transcript.role,
            "content": transcript.content
        })

    def ingest_user_stt(self, text: str):
        """Receives what the user said. Gets routed through priority queue."""
        # Append to context immediately so the UI reflects the user's message with zero delay
        self.context.append({
            "role": "user",
            "content": text
        })
        msg_id = str(uuid.uuid4())
        payload = {"type": "user_stt", "text": text}
        self.message_queue.publish(msg_id, payload)

    def shutdown(self):
        """Gracefully stop background threads."""
        self.message_queue.shutdown()

    # BUG FIX #4: Model list corrected to match the documented architecture.
    # Primary: local Ollama (qwen2.5:7b-instruct) — fast, no cloud cost.
    # Fallback: gemini-2.5-flash (cloud) — the project_analysis.md states
    # "Gemini 3.5 Flash Lite" as the intended Reasoner model; gemini-2.5-flash
    # is the correct current API name. "gemma-4-31b-it" was wrong — that is an
    # open-weights base model, not a hosted Gemini endpoint.
    REASONER_MODELS = ["gemini-3.5-flash-lite", "local-qwen"]

    def _analyze_and_direct(self, _validation_retry: int = 0):
        """
        Core logic: look at context, state, inflight tools -> emit tool calls or Talker directives.

        _validation_retry is an internal counter used to cap self-correction loops
        when the LLM repeatedly emits an invalid tool call format.
        """
        import requests
        import time as _time
        
        system_msg = self.system_instruction or (
            "You are the Director. Analyze context and decide if you need to use tools "
            "or instruct the Talker."
        )
        
        # Build Ollama-compatible messages list.
        # The system prompt always sits at index 0 as a "system" role message.
        # All subsequent entries use only "user" or "assistant" roles so that
        # every API (Ollama, Gemini) accepts the conversation without complaint.
        messages = [{"role": "system", "content": system_msg}]
        prompt_text = ""
        for msg in self.context[-15:]:
            raw_role = msg["role"]
            # Map non-standard roles ("system", "talker") to "user" for the
            # multi-turn API call. The content prefix already identifies the source.
            if raw_role in ("system", "talker"):
                api_role = "user"
            elif raw_role == "assistant":
                api_role = "assistant"
            else:
                api_role = "user"
            messages.append({"role": api_role, "content": msg["content"]})
            prompt_text += f"{raw_role.upper()}: {msg['content']}\n"
            
        log_reasoner_internal("Reasoner Engine", "LLM", prompt_text)
        
        max_retries = 3
        response_text = None

        for model_name in self.REASONER_MODELS:
            for attempt in range(max_retries):
                try:
                    if model_name.startswith("local-"):
                        # Use local Ollama
                        resp = requests.post(
                            "http://localhost:11434/v1/chat/completions",
                            json={
                                "model": "qwen2.5:7b-instruct",
                                "messages": messages,
                                "response_format": {"type": "json_object"},
                                "temperature": 0.1
                            },
                            timeout=30.0
                        )
                        resp.raise_for_status()
                        response_text = resp.json()["choices"][0]["message"]["content"]
                        break
                    else:
                        # Use cloud Google GenAI SDK
                        if not self.client:
                            raise Exception("No API key for Reasoner cloud models.")
                        resp = self.client.models.generate_content(
                            model=model_name,
                            contents=prompt_text,
                            config=types.GenerateContentConfig(
                                system_instruction=system_msg,
                                response_mime_type="application/json",
                            )
                        )
                        response_text = resp.text
                        break
                except Exception as e:
                    wait = 0.5 * (2 ** attempt)
                    logger.warning(
                        f"Reasoner LLM attempt {attempt+1}/{max_retries} on {model_name} "
                        f"failed: {e}. Retrying in {wait}s..."
                    )
                    _time.sleep(wait)
                    response_text = None

            if response_text is not None:
                break
            logger.warning(f"All retries exhausted for {model_name}, trying next model...")

        if response_text is None:
            logger.error("Reasoner: all models and retries exhausted.")
            return
            
        try:
            log_reasoner_internal("LLM", "Reasoner Engine", response_text)
            data = json.loads(response_text)
            
            # {} means no action needed — correct and expected for small talk
            if not data:
                logger.info("Reasoner: no action needed (empty response).")
                return
            
            if "tool_call" in data and data["tool_call"]:
                tc = data["tool_call"]

                # BUG FIX #5: Normalize slots to a list BEFORE validation so the
                # validator and the ToolManager both see the same canonical form.
                # Previously slots were normalized only after a successful validate(),
                # meaning the validator could never inspect the actual slot structure.
                slots = tc.get("slots", [])
                if isinstance(slots, dict):
                    slots = [slots]
                tc["slots"] = slots  # mutate in-place so validator sees normalised form

                is_valid, err = self.tool_validator.validate(tc)
                if is_valid:
                    tool_id = f"t-{uuid.uuid4().hex[:6]}"
                    self.tool_manager.execute_tool(tool_id, tc["name"], slots)
                    # BUG FIX #2 (cont.): use "user" role with a clear prefix so the
                    # execution confirmation is visible to the LLM in the next turn.
                    self.context.append({
                        "role": "user",
                        "content": f"[TOOL DISPATCHED]: {tc['name']} (id={tool_id})"
                    })
                else:
                    # BUG FIX #3: Guard against infinite recursion on persistent
                    # validator failures. The Reasoner instructions allow at most 2
                    # self-correction attempts; beyond that we report and abort.
                    if _validation_retry >= _MAX_VALIDATION_RETRIES:
                        logger.error(
                            f"Reasoner: tool validation failed after {_MAX_VALIDATION_RETRIES} "
                            f"self-corrections. Giving up. Last error: {err}"
                        )
                        dm = DirectorMessage(
                            type="say",
                            content="I ran into a technical issue processing that request. Could you try again?"
                        )
                        self.on_direct_callback(dm)
                        return

                    logger.warning(f"Tool validation error (attempt {_validation_retry + 1}): {err}")
                    self.context.append({
                        "role": "user",
                        "content": f"[TOOL VALIDATION ERROR]: {err}. Fix the tool call and retry."
                    })
                    self._analyze_and_direct(_validation_retry=_validation_retry + 1)
                    return
            
            if "directive" in data and data["directive"]:
                dir_data = data["directive"]
                dm = DirectorMessage(
                    type=dir_data.get("type", "note"),
                    content=dir_data.get("content", "")
                )
                self.on_direct_callback(dm)
                
        except Exception as e:
            logger.error(f"Reasoner error processing response: {e}")
