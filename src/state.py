import time
import threading
from typing import List, Any, Optional, Callable
from dataclasses import dataclass, field
from copy import deepcopy

@dataclass
class ToolCall:
    """Represents a single tool invocation tracked by the state."""
    id: str
    type: str
    status: str
    tick: int
    slots: List[Any] = field(default_factory=list)
    result: Optional[Any] = None


class StateAPI:
    """
    Centralized, thread-safe state management for the Voice Agent.
    
    Concurrency model:
    - All reads return copies (not references) to prevent mutation outside the lock.
    - All writes acquire the lock, mutate, release the lock, THEN notify the Reasoner.
    - This prevents deadlocks when the Reasoner's callback tries to read state.
    """
    def __init__(self, state_id: str):
        self._id = state_id
        self._intent = ""
        self._tool_calls: List[ToolCall] = []
        self._current_focus = ""
        self._last_modified = self._get_current_tick()
        self._lock = threading.RLock()
        self._reasoner_callback: Optional[Callable[[str, Any], None]] = None

    def set_reasoner_callback(self, callback: Callable[[str, Any], None]):
        """Sets a direct messaging callback to notify the Reasoner of external state changes."""
        with self._lock:
            self._reasoner_callback = callback

    def _notify_reasoner(self, source: str, change_type: str, data: Any):
        """Directly messages the Reasoner if the mutation source isn't the Reasoner itself."""
        if source != "reasoner" and self._reasoner_callback:
            self._reasoner_callback(change_type, data)

    def _get_current_tick(self) -> int:
        return int(time.time() * 1000)
    
    def _update_last_modified(self):
        self._last_modified = self._get_current_tick()

    # --- Read Functions ---
    # All reads return copies to prevent external mutation of internal state.

    @property
    def state_id(self) -> str:
        return self._id

    @property
    def intent(self) -> str:
        with self._lock:
            return self._intent

    @property
    def current_focus(self) -> str:
        with self._lock:
            return self._current_focus
            
    @property
    def last_modified(self) -> int:
        with self._lock:
            return self._last_modified

    def get_tool_info(self, tool_id: str) -> Optional[ToolCall]:
        """Returns a deep copy of the ToolCall, safe to use outside the lock."""
        with self._lock:
            for tool in self._tool_calls:
                if tool.id == tool_id:
                    return deepcopy(tool)
        return None

    def get_tool_status(self, tool_id: str) -> Optional[str]:
        tool = self.get_tool_info(tool_id)
        return tool.status if tool else None

    def peek_tool(self) -> Optional[ToolCall]:
        """Returns a deep copy of the most recent tool call."""
        with self._lock:
            return deepcopy(self._tool_calls[-1]) if self._tool_calls else None

    def get_all_tools(self) -> List[ToolCall]:
        """Returns a deep copy of all tool calls."""
        with self._lock:
            return deepcopy(self._tool_calls)

    # --- Update Functions ---

    def set_intent(self, intent: str, source: str = "reasoner"):
        with self._lock:
            self._intent = intent
            self._update_last_modified()
        self._notify_reasoner(source, "intent_changed", {"intent": intent})

    def set_curr_focus(self, focus: str, source: str = "reasoner"):
        with self._lock:
            self._current_focus = focus
            self._update_last_modified()
        self._notify_reasoner(source, "focus_changed", {"focus": focus})
        
    def add_tool_call(self, tool_id: str, tool_type: str, status: str, slots: Optional[List[Any]] = None, source: str = "reasoner"):
        with self._lock:
            new_tool = ToolCall(
                id=tool_id, 
                type=tool_type, 
                status=status, 
                tick=self._get_current_tick(), 
                slots=slots or []
            )
            self._tool_calls.append(new_tool)
            self._update_last_modified()
        self._notify_reasoner(source, "tool_added", {"tool_id": tool_id, "type": tool_type, "status": status})

    def update_tool_status(self, tool_id: str, new_status: str, result: Optional[Any] = None, source: str = "reasoner"):
        notify = False
        with self._lock:
            for tool in self._tool_calls:
                if tool.id == tool_id:
                    tool.status = new_status
                    if result is not None:
                        tool.result = result
                    tool.tick = self._get_current_tick()
                    self._update_last_modified()
                    notify = True
                    break
        
        if notify:
            self._notify_reasoner(source, "tool_status_updated", {"tool_id": tool_id, "status": new_status, "result": result})
