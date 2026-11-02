"""Frozen contract index: name -> (model, version, producer, consumers).

Changing a model's shape requires a new version and an M00 interface proposal.
Contract set 1.2 is additive: the 30 set-1 contracts are unchanged (pinned by
``schemas/v1/contract-set-1.0.snapshot.json``), the 17 set-1.1 contracts are
unchanged, and ``capsule_v3`` is added for external environments.
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
from .analyst import EvidenceInterpretation, RepairSuggestion
from .tools import ToolDefinition, ToolInvocation, ToolResult
from .world import (CapsuleV2, WorldCampaignRecord, WorldCampaignSpec, WorldExperimentProposal,
                    WorldInvariantFinding, WorldMemoryQuery, WorldMemoryRecord, WorldRealityDiff,
                    WorldRunResult, WorldScenario, WorldToolDefinition, WorldToolInvocation,
                    WorldToolResult, WorldTrustedScope)
from .capsule_v3 import CapsuleV3
from .world_packs import WorldPack

CONTRACT_SET_VERSION = "ghostlab-contracts/1.2"


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
    # --- contract set 1.1 (additive): world packs and Analyst outputs ---------------
    Contract("world_pack", WorldPack, 1, "M00", ("M02", "M05", "M06", "M08")),
    Contract("world_campaign_spec", WorldCampaignSpec, 1, "M07", ("M05", "M08")),
    Contract("world_campaign_record", WorldCampaignRecord, 1, "M05", ("M04", "M07", "M08")),
    Contract("world_trusted_scope", WorldTrustedScope, 1, "M05", ("M02", "M06")),
    Contract("world_tool_definition", WorldToolDefinition, 1, "M02", ("M01", "M06")),
    Contract("world_tool_invocation", WorldToolInvocation, 1, "M05", ("M02", "M06")),
    Contract("world_tool_result", WorldToolResult, 1, "M02", ("M01", "M05")),
    Contract("world_scenario", WorldScenario, 1, "M05", ("M02", "M09")),
    Contract("world_experiment_proposal", WorldExperimentProposal, 1, "M01", ("M05", "M06"),
             model_facing=True),
    Contract("world_run_result", WorldRunResult, 1, "M02", ("M04", "M05", "M07", "M08", "M09")),
    Contract("world_reality_diff", WorldRealityDiff, 1, "M02", ("M07", "M08", "M09")),
    Contract("world_invariant_finding", WorldInvariantFinding, 1, "M02",
             ("M03", "M04", "M08", "M09")),
    Contract("capsule_v2", CapsuleV2, 1, "M02", ("M04", "M07", "M08", "M09")),
    Contract("capsule_v3", CapsuleV3, 1, "M00", ("M04", "M07", "M08", "M09")),
    Contract("world_memory_record", WorldMemoryRecord, 1, "M04", ("M03", "M05")),
    Contract("world_memory_query", WorldMemoryQuery, 1, "M03", ("M04",)),
    Contract("evidence_interpretation", EvidenceInterpretation, 1, "M01", ("M05", "M08", "M09"),
             model_facing=True),
    Contract("repair_suggestion", RepairSuggestion, 1, "M01", ("M05", "M06", "M08"),
             model_facing=True),
)

BY_NAME = {contract.name: contract for contract in CONTRACTS}
