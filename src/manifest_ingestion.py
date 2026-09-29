import json
from typing import List, Dict, Any

class ManifestIngestor:
    """
    Handles the system initialization stage by parsing the raw tool manifest.
    Uses an LLM to categorize tools and generate inverse functions for state-modifying tools.
    """
    def __init__(self, llm_client=None):
        # llm_client would be our interface to Gemini (e.g., google.generativeai)
        self.llm_client = llm_client

    def ingest(self, raw_manifest: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Takes the raw tool manifest, processes it via an LLM to categorize 
        and generate inverse tools, and returns the schemas for V2V and Reasoner.
        """
        # 1. Build the prompt with caution pretext
        prompt = self._build_ingestion_prompt(raw_manifest)
        
        # 2. Call the LLM to get the structured schema
        # In actual implementation, we'd parse the LLM JSON response here.
        # structured_schema = self._call_llm(prompt)
        structured_schema = self._simulate_llm_response(raw_manifest)
        
        # 3. Create the simplified summary for the Fast Talker (V2V)
        v2v_summary = self._generate_v2v_summary(structured_schema)
        
        return {
            "v2v_tool_summary": v2v_summary,          # "what tools exist"
            "reasoner_tool_schema": structured_schema # The full schema for the LLM
        }

    def _build_ingestion_prompt(self, raw_manifest: List[Dict[str, Any]]) -> str:
        return f"""
        CAUTION PRETEXT: You are a system initialization agent tasked with meta-programming. 
        You are given a raw tool manifest. You must categorize these tools into:
        1. 'read_only': Tools that do not modify state.
        2. 'state_modifying': Tools that change state.

        For EVERY 'state_modifying' tool, you must generate an 'inverse' tool schema that undoes its action.
        If a tool inherently cannot have an inverse, you must generate a fallback tool schema that simply 
        returns the exact string: "no tool inverse exist for this tool, inform the user the action cannot be taken back".
        
        Raw Manifest:
        {json.dumps(raw_manifest, indent=2)}
        
        Return the result as a structured JSON object containing 'read_only' and 'state_modifying' arrays. 
        Each state_modifying tool should have its corresponding 'inverse_tool' nested within it.
        """

    def _generate_v2v_summary(self, structured_schema: Dict[str, Any]) -> str:
        """Generates a high-level string of 'what tools exist' for the Fast Talker."""
        tool_names = []
        for tool in structured_schema.get("read_only", []):
            tool_names.append(tool.get("name", ""))
            
        for tool in structured_schema.get("state_modifying", []):
            tool_names.append(tool.get("name", ""))
            
        # Filter out empty names and format
        valid_names = [name for name in tool_names if name]
        return f"Available tools you can suggest: {', '.join(valid_names)}"

    def _simulate_llm_response(self, raw_manifest: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Deterministic stand-in for the real LLM categorisation.
        Categorises tools based on the 'modifies_state' flag in the manifest.
        Used for testing until the real LLM client is wired up.
        """
        read_only = []
        state_modifying = []

        for tool in raw_manifest:
            entry = {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {}),
            }
            if tool.get("modifies_state", False):
                entry["inverse_tool"] = {
                    "name": f"undo_{tool.get('name', '')}",
                    "description": f"Reverses the effect of {tool.get('name', '')}",
                }
                state_modifying.append(entry)
            else:
                read_only.append(entry)

        return {
            "read_only": read_only,
            "state_modifying": state_modifying,
        }
