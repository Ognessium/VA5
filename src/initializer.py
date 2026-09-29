import os
import json
from src.system import VoiceAgentSystem

class AgentInitializer:
    """
    Handles all the system initialization tasks:
    - Loading and templating instruction files
    - Instantiating the VoiceAgentSystem
    - Providing the formatted prompts for the Talker and Reasoner
    """
    def __init__(self, manifest_path: str, base_dir: str):
        self.manifest_path = manifest_path
        self.base_dir = base_dir
        
        with open(manifest_path, "r") as f:
            self.manifest = json.load(f)
            
    def generate_talker_tool_list(self, tools: list) -> str:
        import logging
        import requests
        from google import genai
        from google.genai import types

        prompt = f"""
You are configuring the tools for a voice assistant.
Translate the following JSON tool manifest into the following exact format for each tool:

TOOL: [name]
does: [brief description in plain words]
kind: [lookup or action]
returns: [what the result looks like, e.g., options to choose from, or confirmation]
needs_from_user:
  - [arg_name] -> ask: "[short question]"
working (examples, vary them): "[phrase 1]", "[phrase 2]"
dont_imply: [words you shouldn't use, e.g., booked, confirmed, done, latest, catalog]

Rules:
1. Flatten arguments into plain phrases. Drop any system-filled arguments.
2. If the tool modifies state, it's an action, else it's a lookup.
3. If it returns a list, it's options. If it modifies state, it's a confirmation.
4. For 'working' phrases, DO NOT use completion words like booked, confirmed, done, sent, reserved, cancelled, found.
5. Provide a block for every tool in the manifest. Separate blocks by a blank line.

Manifest:
{json.dumps(tools, indent=2)}
"""
        forbidden_words = ["booked", "confirmed", "done", "sent", "reserved", "cancelled", "found", "latest", "catalog", "options"]
        raw_output = None
        # 1. Try Cloud GenAI First
        try:
            client = genai.Client(
                http_options=types.HttpOptions(
                    timeout=30000, 
                    retry_options=types.HttpRetryOptions(attempts=1) 
                )
            )
            response = client.models.generate_content(
                model="gemma-4-26b-a4b-it",
                contents=prompt,
            )
            raw_output = response.text
        except Exception as e:
            logging.warning(f"GenAI initializer failed: {e}. Trying local Ollama...")

        # 2. Try Local Ollama if Cloud failed
        if raw_output is None:
            try:
                resp = requests.post(
                    "http://localhost:11434/v1/chat/completions",
                    json={
                        "model": "qwen2.5:7b-instruct",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1
                    },
                    timeout=60.0
                )
                if resp.status_code == 200:
                    raw_output = resp.json()["choices"][0]["message"]["content"]
            except Exception as e:
                logging.warning(f"Local Ollama initializer failed: {e}. Falling back to simple list.")

        # 3. Validation & Fallback
        if raw_output:
            failed = False
            for line in raw_output.splitlines():
                if line.startswith("working (examples"):
                    for word in forbidden_words:
                        if word in line.lower():
                            failed = True
                            break
            if not failed:
                return raw_output
            logging.warning("Validation failed (forbidden words used). Falling back to simple list.")

        
        fallback_blocks = []
        for t in tools:
            name = t['name']
            desc = t.get('description', '')
            clean_name = name.replace('_', ' ')
            fallback_blocks.append(f"TOOL: {name}\ndoes: {desc}\nworking (examples, vary them): \"working on {clean_name}\"\ndont_imply: booked, confirmed, done, sent\n")
        return "\n".join(fallback_blocks)
            
    def build_prompts(self) -> tuple[str, str]:
        with open(os.path.join(self.base_dir, "reasoner_instructions.md"), "r") as f:
            reasoner_prompt = f.read()
        with open(os.path.join(self.base_dir, "talker_instructions.md"), "r") as f:
            talker_prompt = f.read()
            
        state_api_desc = "Intent (string), Current Focus (string), Tool Calls (list of tools with id, type, status, result)."
        output_format = '{"tool_call": {"name": "tool_name", "slots": [{"arg1": "val1"}]}, "directive": {"type": "SAY", "content": "..."}} (or return {} if no action is needed, raw JSON only)'
        
        reasoner_prompt = reasoner_prompt.replace("{{TOOL_MANIFEST}}", json.dumps(self.manifest))
        reasoner_prompt = reasoner_prompt.replace("{{STATE_API}}", state_api_desc)
        reasoner_prompt = reasoner_prompt.replace("{{TALKER_MESSAGE_FORMAT}}", "[SYSTEM:SAY], [SYSTEM:NOTE], [SYSTEM:INSTRUCTION]")
        reasoner_prompt = reasoner_prompt.replace("{{OUTPUT_FORMAT}}", output_format)
        
        tools = self.manifest if isinstance(self.manifest, list) else self.manifest.get("tools", [])
        tool_list = self.generate_talker_tool_list(tools)
        talker_prompt = talker_prompt.replace("{{TOOL_LIST}}", tool_list)
        
        return reasoner_prompt, talker_prompt

    def initialize_system(self) -> VoiceAgentSystem:
        reasoner_prompt, _ = self.build_prompts()
        system = VoiceAgentSystem(raw_manifest=self.manifest)
        system.reasoner.system_instruction = reasoner_prompt
        return system
