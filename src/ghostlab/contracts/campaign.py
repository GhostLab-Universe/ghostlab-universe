"""Campaigns, tasks and the AgentEnvelope used for internal role communication."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal

from pydantic import Field, model_validator

from .budgets import BudgetLimits, BudgetUsage
from .common import ErrorInfo, Principal, Ref, SchemaRef, TrustedScope
from .primitives import (AgentRole, Identifier, SchemaVersion, ShortText, SpanId, StrictModel,
                         Text, TraceId, UtcTimestamp, WorldVersion, parse_timestamp,
                         require_unique)

CampaignStatus = Literal["queued", "running", "awaiting_approval", "completed", "failed",
                         "cancelled", "budget_exhausted"]
TaskStatus = Literal["pending", "ready", "running", "awaiting_approval", "succeeded",
                     "failed", "cancelled", "skipped"]
TaskKind = Literal["propose_world", "propose_experiment", "validate_proposal", "execute_run",
                   "verify_run", "analyze_evidence", "record_memory", "await_approval"]

# Legal task transitions. M05 enforces them; terminal states have no exits.
TASK_TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType({
    "pending": frozenset({"ready", "skipped", "cancelled"}),
    "ready": frozenset({"running", "skipped", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "awaiting_approval", "ready", "cancelled"}),
    "awaiting_approval": frozenset({"running", "failed", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "skipped": frozenset(),
})


class CampaignSpec(StrictModel):
    """What a client asks for. Project and principal come from the session, not here."""

    schema_version: SchemaVersion
    objective: Text
    success_criteria: list[ShortText] = Field(min_length=1, max_length=16)
    world_version: WorldVersion
    budget: BudgetLimits


class CampaignRecord(StrictModel):
    schema_version: SchemaVersion
    campaign_id: Identifier
    project_id: Identifier
    created_by: Principal
    spec: CampaignSpec
    status: CampaignStatus
    usage: BudgetUsage
    trace_id: TraceId
    created_at: UtcTimestamp
    updated_at: UtcTimestamp

    @model_validator(mode="after")
    def _check(self):
        if parse_timestamp(self.updated_at) < parse_timestamp(self.created_at):
            raise ValueError("updated_at precedes created_at")
        return self


class TaskSpec(StrictModel):
    schema_version: SchemaVersion
    task_id: Identifier
    campaign_id: Identifier
    kind: TaskKind
    role: AgentRole
    depends_on: list[Identifier] = Field(max_length=32)
    status: TaskStatus
    attempt: int = Field(ge=0, le=100)
    max_attempts: int = Field(ge=1, le=100)
    exit_condition: Text
    output_ref: Ref | None

    @model_validator(mode="after")
    def _check(self):
        require_unique(self.depends_on, "depends_on")
        if self.task_id in self.depends_on:
            raise ValueError("A task cannot depend on itself")
        if self.attempt > self.max_attempts:
            raise ValueError("attempt exceeds max_attempts")
        return self


EnvelopeStatus = Literal["pending", "in_progress", "completed", "failed", "rejected",
                         "cancelled"]


class AgentEnvelope(StrictModel):
    """Typed message between controller and logical roles.

    ``scope`` is attached by the controller. Content produced by a model role is
    carried by reference in ``evidence`` and never alters ``scope``.
    """

    schema_version: SchemaVersion
    envelope_id: Identifier
    correlation_id: Identifier
    campaign_id: Identifier
    task_id: Identifier
    trace_id: TraceId
    span_id: SpanId
    parent_envelope_id: Identifier | None
    sender_role: AgentRole
    recipient_role: AgentRole
    scope: TrustedScope
    objective: Text
    input_refs: list[Ref] = Field(max_length=64)
    expected_output: SchemaRef
    status: EnvelopeStatus
    evidence: list[Ref] = Field(max_length=256)
    errors: list[ErrorInfo] = Field(max_length=32)
    created_at: UtcTimestamp

    @model_validator(mode="after")
    def _check(self):
        if self.scope.role != self.recipient_role:
            raise ValueError("scope must be issued for the recipient role")
        if self.status == "failed" and not self.errors:
            raise ValueError("A failed envelope must carry errors")
        return self
