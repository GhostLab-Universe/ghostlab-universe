"""Track B: developer-owned target-agent integration.

Stdlib-only. Reuses RefundWorld-style tool protocol (tool name + arguments
dict) without modifying core worlds. Track A interface was unavailable at
build time (prior results gap); this module defines a minimal duck-typed
world protocol and documents the gap. See README in the equipment example.
"""

from .gateway import ControlledGateway
from .qualify import QualifyReport, qualify_agent
from .registry import AgentManifest, AgentRecord, TargetRegistry

__all__ = [
    "AgentManifest",
    "AgentRecord",
    "ControlledGateway",
    "QualifyReport",
    "TargetRegistry",
    "qualify_agent",
]
