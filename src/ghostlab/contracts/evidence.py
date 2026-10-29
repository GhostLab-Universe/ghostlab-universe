"""Run evidence, Reality Diff and Failure Capsule v1.

The *V1 / mirror types match what ``ghostlab.core`` and ``ghostlab.capsule``
emit today, so existing outputs and v1 capsules validate unchanged. Shape
validation here does not replace the core's checksum and replay checks.
"""

from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints, model_validator

from .common import Ref
from .primitives import (Identifier, MinorUnits, Sha256Hex, StrictModel, Text, TraceId,
                         UtcTimestamp, WorldVersion, require_unique)
from .tools import LedgerRow, OrderRecord, ToolArg, ToolOutput

InvariantName = Literal["authorized_refunds", "total_refund_within_payment",
                        "no_duplicate_full_refund", "unique_idempotency_keys"]
EventId = Annotated[int, Field(ge=1, le=1_000)]


class FixtureV1(StrictModel):
    order_id: ToolArg
    customer_id: ToolArg
    paid_minor: int = Field(ge=1, le=10**12)
    currency: ToolArg


class ScenarioV1(StrictModel):
    timeout_after_commit: bool
    max_tool_calls: int = Field(ge=1, le=1_000)


class WorldSnapshot(StrictModel):
    order: OrderRecord
    ledger: list[LedgerRow] = Field(max_length=1_000)
    total_refunded_minor: MinorUnits


class RecordedRequest(StrictModel):
    """What the target attempted, recorded even when the world rejected it."""

    tool: Annotated[str, StringConstraints(max_length=200)]
    arguments: dict[str, JsonValue]


class OutcomeResult(StrictModel):
    result: ToolOutput


class OutcomeError(StrictModel):
    error: Literal["ToolError", "PaymentTimeout", "BudgetExceeded"]
    message: Text


class TraceEvent(StrictModel):
    event_id: EventId
    request: RecordedRequest
    outcome: OutcomeResult | OutcomeError
    state: WorldSnapshot


class RefundChecks(StrictModel):
    authorized_refunds: bool
    total_refund_within_payment: bool
    no_duplicate_full_refund: bool
    unique_idempotency_keys: bool


class RunResult(StrictModel):
    adapter: Literal["vulnerable", "repaired"]
    adapter_version: Literal["refund-adapters/1"]
    actor_id: ToolArg
    status: Literal["completed", "budget_exhausted"]
    trace: list[TraceEvent] = Field(max_length=1_000)
    final_state: WorldSnapshot
    checks: RefundChecks


class Divergence(StrictModel):
    event_id: EventId
    baseline: TraceEvent | None
    faulted: TraceEvent | None


class RealityDiff(StrictModel):
    first_divergence: Divergence | None
    baseline_refunded_minor: MinorUnits
    faulted_refunded_minor: MinorUnits
    violated_invariants: list[InvariantName]


class ExperimentResult(StrictModel):
    baseline: RunResult
    faulted: RunResult
    repaired: RunResult
    diff: RealityDiff


class CapsulePayloadV1(StrictModel):
    schema_version: Literal[1]
    world_version: WorldVersion
    fixture: FixtureV1
    scenario: ScenarioV1
    actor_id: ToolArg
    source: Literal["trusted-synthetic-refund-adapter"]
    seed: int = Field(ge=0, le=2**53 - 1)  # JS-safe integer
    recorded_model_outputs: list[JsonValue] = Field(max_length=1_000)
    run: RunResult

    @model_validator(mode="after")
    def _check(self):
        if self.run.actor_id != self.actor_id:
            raise ValueError("Recorded principal does not match capsule")
        if len(self.run.trace) > self.scenario.max_tool_calls:
            raise ValueError("Trace longer than the scenario's tool budget")
        return self


class CapsuleV1(StrictModel):
    """On-disk v1 envelope. sha256 is accidental-change detection, not authorship."""

    payload: CapsulePayloadV1
    sha256: Sha256Hex


class InvariantFinding(StrictModel):
    """A verifier verdict derived from ledger/state evidence, never model confidence."""

    schema_version: Literal[1]
    finding_id: Identifier
    run_id: Identifier
    invariant: InvariantName
    verifier_version: Identifier
    verdict: Literal["passed", "failed"]
    evidence_event_ids: list[EventId] = Field(max_length=1_000)
    observed_minor: MinorUnits | None
    limit_minor: MinorUnits | None
    recorded_at: UtcTimestamp

    @model_validator(mode="after")
    def _check(self):
        require_unique(self.evidence_event_ids, "evidence_event_ids")
        if self.verdict == "failed" and not self.evidence_event_ids:
            raise ValueError("A failed invariant must cite evidence events")
        return self


ExperimentEventKind = Literal[
    "campaign_created", "campaign_status_changed", "task_status_changed", "run_started",
    "tool_event_recorded", "run_completed", "finding_recorded", "approval_requested",
    "approval_decided", "budget_reserved", "budget_exhausted", "capsule_exported",
    "replay_completed",
]


class ExperimentEvent(StrictModel):
    """Ordered application event for status streams; not model-token streaming."""

    schema_version: Literal[1]
    campaign_id: Identifier
    sequence: int = Field(ge=1, le=10**12)
    kind: ExperimentEventKind
    task_id: Identifier | None
    run_id: Identifier | None
    trace_id: TraceId
    occurred_at: UtcTimestamp
    refs: list[Ref] = Field(max_length=64)
    summary: Text
