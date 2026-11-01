"""External target-agent routing (P3): plain-Python agents, controlled gateway.

Self-play proof: deterministic scripted agents on both sides of the
interface, zero live calls. External agents implement
:class:`ExternalTarget` (plain mappings, no GhostLab imports);
:class:`ExternalTargetAdapter` routes one through the frozen
``ghostlab-extensions/1`` target protocol so every proposal crosses a
:class:`ControlledGateway` before it can reach a world. Evidence records
keep three outcomes distinct -- ``agent_retest``, ``recorded_replay`` and
``boundary_enforcement`` -- with Explorer, Target and Verifier identities
recorded separately. Standard library only.
"""

from .evidence import (AGENT_RETEST, BOUNDARY_ENFORCEMENT, OUTCOME_KINDS, RECORDED_REPLAY,
                       replay_recorded, run_external_attempt, summarize)
from .external import (ControlledGateway, ExternalTarget, ExternalTargetAdapter,
                       GatewayDenial, check_external_target)

__all__ = [
    "AGENT_RETEST", "BOUNDARY_ENFORCEMENT", "OUTCOME_KINDS", "RECORDED_REPLAY",
    "ControlledGateway", "ExternalTarget", "ExternalTargetAdapter", "GatewayDenial",
    "check_external_target", "replay_recorded", "run_external_attempt", "summarize",
]
