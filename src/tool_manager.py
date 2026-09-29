import time
import threading
import logging
from typing import List, Dict, Any, Optional, Callable
from src.state import StateAPI

logger = logging.getLogger(__name__)

class ToolManager:
    """
    Handles calling, tracking, and canceling tools.
    Receives tool feedback directly and updates the state.
    
    Thread-safety: _inflight_tools is guarded by its own lock since
    execute_tool, cancel_tool, and handle_tool_feedback can be called
    from different threads (Reasoner thread, async API callback thread).
    
    Optional hooks (set after construction for benchmark integration):
      tool_executor:    (tool_name: str, slots: list) -> dict
                        If set, called instead of the default 2-second mock timer.
                        Must return the tool result dict.
      telemetry_logger: (tool_name: str, kwargs: dict, t_start: float, t_end: float) -> None
                        If set, called after each tool execution with timing data.
    """
    def __init__(self, state_api: StateAPI):
        self._state = state_api
        self._inflight_tools: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.tool_executor: Optional[Callable] = None
        self.telemetry_logger: Optional[Callable] = None

    def get_inflight_tools(self) -> List[Dict[str, Any]]:
        """Returns a snapshot of currently in-flight tools."""
        with self._lock:
            return list(self._inflight_tools.values())

    def _slots_to_kwargs(self, slots: List[Any]) -> Dict[str, Any]:
        """Convert various slot formats to a flat kwargs dict."""
        kwargs = {}
        if isinstance(slots, dict):
            return slots
        for slot in slots:
            if isinstance(slot, dict):
                if "name" in slot and "value" in slot:
                    # Format: [{"name": "dest", "value": "London"}, ...]
                    kwargs[slot["name"]] = slot["value"]
                else:
                    # Format: [{"dest": "London", "date": "..."}]
                    kwargs.update(slot)
        return kwargs

    def execute_tool(self, tool_id: str, tool_type: str, slots: List[Any]):
        """Submits a tool for execution via the external tool API."""
        tool_data = {
            "id": tool_id,
            "type": tool_type,
            "slots": slots
        }
        with self._lock:
            self._inflight_tools[tool_id] = tool_data
        
        # Update state. source="tool_manager" triggers notification to Reasoner.
        self._state.add_tool_call(tool_id, tool_type, "in-flight", slots, source="tool_manager")
        
        if self.tool_executor:
            # Real dispatch via provided executor (e.g., MockAPIRegistry)
            def _execute():
                kwargs = self._slots_to_kwargs(slots)
                t_start = time.time()
                try:
                    result = self.tool_executor(tool_type, kwargs)
                    t_end = time.time()
                    if self.telemetry_logger:
                        self.telemetry_logger(tool_type, kwargs, t_start, t_end)
                    logger.info(f"Tool '{tool_type}' executed in {t_end - t_start:.2f}s → {result}")
                    self.handle_tool_feedback(tool_id, "success", result)
                except Exception as e:
                    logger.error(f"Tool '{tool_type}' failed: {e}")
                    self.handle_tool_feedback(tool_id, "failed", {"error": str(e)})
            threading.Thread(target=_execute, daemon=True).start()
        else:
            # Default mock dispatch: send success feedback after 2 seconds
            def mock_response():
                self.handle_tool_feedback(tool_id, "success", {"mock": "result"})
            threading.Timer(2.0, mock_response).start()

    def cancel_tool(self, tool_id: str):
        """Cancels an in-flight tool."""
        with self._lock:
            removed = self._inflight_tools.pop(tool_id, None)
        
        if removed:
            self._state.update_tool_status(tool_id, "cancelled", source="tool_manager")
            # TODO: send cancellation to external tool API

    def handle_tool_feedback(self, tool_id: str, status: str, result: Optional[Any] = None):
        """
        Receives direct feedback (success, failed, etc.) from the external tool API.
        Terminal statuses remove the tool from in-flight tracking.
        """
        terminal_statuses = {"success", "failed", "cancelled"}
        
        with self._lock:
            if status in terminal_statuses:
                self._inflight_tools.pop(tool_id, None)
            
        self._state.update_tool_status(tool_id, status, result=result, source="tool_manager")

