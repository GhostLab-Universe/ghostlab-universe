"""Frozen contract index: name -> (model, version, producer, consumers).

Changing a model's shape requires a new version and an M00 interface proposal.
"""

from dataclasses import dataclass

from .approvals import ApprovalDecision, ApprovalRequest, ApprovalSubmission
from .budgets import BudgetLimits, BudgetReservation, BudgetUsage
from .campaign import AgentEnvelope, CampaignRecord, CampaignSpec, TaskSpec
from .common import ArtifactRef, ErrorInfo, Ref, TrustedScope
from .context import ContextBundle, SkillSpec
from .evidence import (CapsuleV1, ExperimentEvent, ExperimentResult, InvariantFinding,
                       RealityDiff, RunResult)
from .memory import MemoryQuery, MemoryRecord
from .primitives import StrictModel
from .provider import ExperimentProposal, ModelReply, ModelRequest
from .tools import ToolDefinition, ToolInvocation, ToolResult

CONTRACT_SET_VERSION = "ghostlab-contracts/1"


@dataclass(frozen=True)
class Contract:
    name: str
    model: type[StrictModel]
    version: int
    producer: str
    consumers: tuple[str, ...]
    model_facing: bool = False  # requested as model structured output

    @property
    def schema_id(self) -> str:
        return f"urn:ghostlab:contract:{self.name}:{self.version}"


CONTRACTS: tuple[Contract, ...] = (
    Contract("agent_envelope", AgentEnvelope, 1, "M05", ("M03", "M09")),
    Contract("campaign_spec", CampaignSpec, 1, "M07", ("M05", "M08")),
    Contract("campaign_record", CampaignRecord, 1, "M05", ("M04", "M07", "M08")),
    Contract("task_spec", TaskSpec, 1, "M05", ("M04", "M07", "M08")),
    Contract("context_bundle", ContextBundle, 1, "M03", ("M01", "M05")),
    Contract("skill_spec", SkillSpec, 1, "M03", ("M05", "M09")),
    Contract("model_request", ModelRequest, 1, "M05", ("M01",)),
    Contract("model_reply", ModelReply, 1, "M01", ("M05", "M09")),
    Contract("experiment_proposal", ExperimentProposal, 1, "M01", ("M05", "M06"),
             model_facing=True),
    Contract("tool_definition", ToolDefinition, 1, "M02", ("M01", "M06")),
    Contract("tool_invocation", ToolInvocation, 1, "M05", ("M02", "M06")),
    Contract("tool_result", ToolResult, 1, "M02", ("M01", "M05")),
    Contract("run_result", RunResult, 1, "M02", ("M04", "M05", "M07", "M08", "M09")),
    Contract("experiment_result", ExperimentResult, 1, "M02", ("M05", "M07", "M08")),
    Contract("reality_diff", RealityDiff, 1, "M02", ("M07", "M08", "M09")),
    Contract("capsule_v1", CapsuleV1, 1, "M02", ("M04", "M07", "M08", "M09")),
    Contract("invariant_finding", InvariantFinding, 1, "M02", ("M03", "M04", "M08", "M09")),
    Contract("experiment_event", ExperimentEvent, 1, "M05", ("M07", "M08", "M09")),
    Contract("memory_record", MemoryRecord, 1, "M04", ("M03", "M05")),
    Contract("memory_query", MemoryQuery, 1, "M03", ("M04",)),
    Contract("approval_request", ApprovalRequest, 1, "M06", ("M04", "M05", "M07", "M08")),
    Contract("approval_submission", ApprovalSubmission, 1, "M08", ("M07", "M06")),
    Contract("approval_decision", ApprovalDecision, 1, "M06", ("M04", "M05", "M07", "M08")),
    Contract("budget_limits", BudgetLimits, 1, "M06", ("M05", "M07", "M08")),
    Contract("budget_usage", BudgetUsage, 1, "M05", ("M07", "M08", "M09")),
    Contract("budget_reservation", BudgetReservation, 1, "M05", ("M04", "M09")),
    Contract("trusted_scope", TrustedScope, 1, "M05", ("M03", "M06")),
    Contract("ref", Ref, 1, "M00", ("all",)),
    Contract("error_info", ErrorInfo, 1, "M00", ("all",)),
    Contract("artifact_ref", ArtifactRef, 1, "M04", ("M03", "M07", "M08")),
)

BY_NAME = {contract.name: contract for contract in CONTRACTS}
