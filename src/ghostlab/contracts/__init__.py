"""Frozen v1 service/model boundary contracts (Pydantic, strict).

Requires the ``contracts`` extra. ``ghostlab.core`` and ``ghostlab.capsule``
never import this package, so simulation and replay stay dependency-free.
"""

from .approvals import ApprovalDecision, ApprovalRequest, ApprovalSubmission
from .budgets import BudgetLimits, BudgetReservation, BudgetUsage
from .campaign import (TASK_TRANSITIONS, AgentEnvelope, CampaignRecord, CampaignSpec,
                       TaskSpec)
from .common import (ArtifactRef, ErrorInfo, Principal, Provenance, Ref, SchemaRef,
                     TrustedScope)
from .context import ContextBundle, ContextItem, SkillRef, SkillSpec
from .evidence import (CapsuleV1, ExperimentEvent, ExperimentResult, InvariantFinding,
                       RealityDiff, RunResult, TraceEvent)
from .memory import MemoryQuery, MemoryRecord
from .provider import (ExperimentProposal, ModelReply, ModelRequest, ProposedToolCall,
                       Usage)
from .registry import BY_NAME, CONTRACT_SET_VERSION, CONTRACTS, Contract
from .tools import ToolDefinition, ToolInvocation, ToolResult

__all__ = [
    "AgentEnvelope", "ApprovalDecision", "ApprovalRequest", "ApprovalSubmission",
    "ArtifactRef", "BY_NAME", "BudgetLimits", "BudgetReservation", "BudgetUsage",
    "CONTRACTS", "CONTRACT_SET_VERSION", "CampaignRecord", "CampaignSpec", "CapsuleV1",
    "ContextBundle", "ContextItem", "Contract", "ErrorInfo", "ExperimentEvent",
    "ExperimentProposal", "ExperimentResult", "InvariantFinding", "MemoryQuery",
    "MemoryRecord", "ModelReply", "ModelRequest", "Principal", "ProposedToolCall",
    "Provenance", "RealityDiff", "Ref", "RunResult", "SchemaRef", "SkillRef", "SkillSpec",
    "TASK_TRANSITIONS", "TaskSpec", "ToolDefinition", "ToolInvocation", "ToolResult",
    "TraceEvent", "TrustedScope", "Usage",
]
