from dataclasses import dataclass
from typing import Literal

@dataclass
class TalkerTranscript:
    """
    Represents what the Fast Talker said to the user.
    Routed to the Reasoner by the orchestrator (not by the Talker itself).
    """
    content: str
    role: Literal["talker"] = "talker"

@dataclass
class DirectorMessage:
    """
    One-way message from the Slow Reasoner (Director) to the Fast Talker.
    Used to guide the Talker without overwhelming it with full reasoning context.
    
    Types:
    - 'note': Hidden context or fact the Talker should be aware of (e.g., "The user seems frustrated").
    - 'say': Explicit text the Talker must synthesize and speak verbatim.
    - 'instruction': A behavioral command (e.g., "Ask them for their email").
    """
    type: Literal["note", "say", "instruction"]
    content: str
