import os
from typing import List
from src.models.messages import DirectorMessage
from src.state import StateAPI
from google import genai
from google.genai import types

class FastTalker:
    """
    The V2V Fast Talker model. 
    Treated as a highly responsive 'dumb chatterbox'. It receives audio/text 
    and short, concise DirectorMessages from the Reasoner.
    It simply acts on the directives and talks to the user like a good actor.
    """
    def __init__(self, state_api: StateAPI, system_instruction: str = ""):
        self.state_api = state_api
        self.system_instruction = system_instruction
        self.director_context: List[DirectorMessage] = []
        
        api_key = os.environ.get("GOOGLE_API_KEY")
        if api_key:
            self.client = genai.Client(api_key=api_key)
        else:
            self.client = None

    def receive_director_message(self, message: DirectorMessage):
        """
        Ingests a message from the Director (Reasoner).
        This updates the Talker's local context without overwhelming it.
        """
        self.director_context.append(message)
        
    def generate_response(self, user_audio_input: str, history: list = None) -> str:
        """
        Generates the voice response to the user based on context and input.
        """
        if not self.client:
            return "Error: GOOGLE_API_KEY not set."

        # BUG FIX #10: Previously all queued DirectorMessages were joined into a
        # single plain string with spaces, e.g. "Go to Tokyo Fly tomorrow Ask for name".
        # This discarded the message type (SAY / NOTE / INSTRUCTION) entirely, so the
        # text-mode Talker had no way to distinguish a fact it should state now (SAY)
        # from hidden background context (NOTE) or a behavioral correction (INSTRUCTION).
        # Format each message with its prefix so the Talker prompt matches the same
        # [SYSTEM:<TYPE>] contract documented in talker_instructions.md.
        directive_lines = [
            f"[SYSTEM:{m.type.upper()}] {m.content}"
            for m in self.director_context
        ]
        self.director_context.clear()

        prompt = ""
        if history:
            prompt += "Recent Conversation History:\n"
            for msg in history:
                role = "User" if msg["role"] == "user" else "You"
                prompt += f"{role}: {msg['content']}\n\n"

        prompt += f"User said: {user_audio_input}\n"
        if directive_lines:
            prompt += "Director directives:\n" + "\n".join(directive_lines) + "\n"

        sys_inst = self.system_instruction or (
            "You are a fast, conversational voice agent. Keep your responses short and natural. "
            "Follow director directives closely."
        )

        try:
            response = self.client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=sys_inst,
                )
            )
            return response.text
        except Exception as e:
            return f"Talker LLM error: {e}"
