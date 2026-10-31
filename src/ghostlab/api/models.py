"""HTTP response views for the M07 control plane (``ghostlab-api/1``).

These only compose M00 contracts; every record, run, diff, finding, approval, event
and error inside them is the published contract. Refund evidence is mapped onto the
generic world contracts (``ghostlab.contracts.world_refund``), so one response shape
serves every world. Project and principal are never request fields.

Text that originated from a model (a proposal's hypothesis, an event summary) is
untrusted data and must be rendered as data.
"""

from typing import Annotated, Literal

from pydantic import Field, JsonValue

from ghostlab.contracts import (ApprovalDecision, ApprovalRequest, ErrorInfo, ExperimentEvent,
                                Principal, Ref, TaskSpec)
from ghostlab.contracts.approvals import ApprovalStatus
from ghostlab.contracts.primitives import (Count, Identifier, SchemaVersion, Sha256Hex, ShortText,
                                           StrictModel, Text, UtcTimestamp, _exact_type)
from ghostlab.contracts.world import (WorldCampaignRecord, WorldExperimentProposal,
                                      WorldInvariantFinding, WorldRealityDiff, WorldRunResult)
from ghostlab.contracts.world_packs import PackVersion

RunName = Literal["baseline", "fork", "repaired"]
AuthMode = Literal["local_dev", "configured_tokens"]
# "scripted" and "fixture" are checked-in replies, never a model; "disabled" reaches nothing.
InferenceMode = Literal["disabled", "fixture", "scripted", "live"]
RunStatus = Literal["completed", "budget_exhausted"]
NotProduction = Annotated[Literal[False], _exact_type(bool)]


class ErrorResponse(StrictModel):
    """Every non-2xx body. ``reason`` is a stable machine code finer than ``error.code``."""

    error: ErrorInfo
    reason: Identifier | None


class SessionInfo(StrictModel):
    """What the authenticated session resolved to. Nothing here is client-selected."""

    schema_version: SchemaVersion
    principal: Principal
    project_id: Identifier
    auth_mode: AuthMode
    production_authenticated: NotProduction  # no production authentication exists yet
    can_approve: bool
    inference: InferenceMode  # this server's mode; each campaign also carries its own
    runnable_worlds: list[PackVersion] = Field(max_length=16)


class JobStatus(StrictModel):
    """This server's background execution for a campaign, beside the authoritative record.

    ``active``: a worker is running it. ``idle``: paused or finished. ``stalled``: the
    checkpoint is unfinished but no worker holds it (a crash, restart or worker error);
    the owner can continue it with ``resume``.
    """

    state: Literal["active", "idle", "stalled"]
    cancel_requested: bool
    error: ErrorInfo | None


class ApprovalSummary(StrictModel):
    approval_id: Identifier
    action_digest: Sha256Hex
    expires_at: UtcTimestamp


class Coverage(StrictModel):
    """Whether an experiment tested anything. Null only on a checkpoint older than coverage.

    ``complete: false`` means a run stopped at its tool cap or a fault never fired: a
    clean result from it proves nothing, and only the violations it did reach count.
    ``faults_triggered`` is null on a checkpoint from before it was recorded.
    """

    baseline: RunStatus
    fork: RunStatus
    faults_triggered: bool | None
    complete: bool


class ExecutionRecord(StrictModel):
    mode: Literal["lab", "enforced"]
    guarded: bool


class GuardDenial(StrictModel):
    event_id: int = Field(ge=1, le=1_000)
    error: ShortText


class RepairView(StrictModel):
    """The repair's recorded assessment; world-specific fields are null elsewhere.

    Only ``verified`` means the repair held. ``held_by`` is null unless it did: passing
    checks on a run that never got there prove nothing.
    """

    authorized: bool
    verified: bool
    run: Ref | None
    run_status: RunStatus | None
    safety_held: bool | None
    task_complete: bool | None
    task_outcome: dict[str, bool] | None
    adapter: ShortText | None
    execution: ExecutionRecord | None
    guard_denials: list[GuardDenial] | None = Field(max_length=1_000)
    held_by: Literal["adapter", "guarded_boundary"] | None
    lost_faults: list[ShortText] | None = Field(max_length=16)  # met by the fork, not the repair
    refunded_minor: Count | None


class ExperimentSummary(StrictModel):
    experiment_id: Identifier
    iteration: int = Field(ge=1, le=1_000)
    hypothesis: Text  # model output: untrusted data
    adapter_id: ShortText
    coverage: Coverage | None
    runs: list[RunName] = Field(max_length=3)


class CampaignView(StrictModel):
    """Authoritative status from the campaign's checkpoint. Safe to read after reconnect."""

    record: WorldCampaignRecord
    inference: InferenceMode  # what produced this campaign's proposals
    outcome: ShortText | None  # final outcomes are listed in docs/modules/M07.md
    fixture_sha256: Sha256Hex | None  # the pinned fixture; null on an older refund checkpoint
    job: JobStatus
    tasks: list[TaskSpec] = Field(max_length=256)
    approval: ApprovalSummary | None
    experiments: list[ExperimentSummary] = Field(max_length=32)
    repair: RepairView | None
    errors: list[ErrorInfo] = Field(max_length=256)
    event_count: Count


class EventPage(StrictModel):
    """Application events by ascending sequence. Not model-token streaming."""

    campaign_id: Identifier
    inference: InferenceMode
    events: list[ExperimentEvent] = Field(max_length=200)
    next_after: Count  # pass back as ``after`` to continue
    has_more: bool


class ExperimentView(StrictModel):
    experiment_id: Identifier
    inference: InferenceMode
    iteration: int = Field(ge=1, le=1_000)
    proposal: WorldExperimentProposal  # model output, validated: untrusted data
    execution: ExecutionRecord | None
    coverage: Coverage | None
    diff: WorldRealityDiff
    findings: list[WorldInvariantFinding] = Field(max_length=256)
    runs: list[RunName] = Field(max_length=3)


class RealityDiffView(StrictModel):
    """A diff with its coverage: alone, a truncated run's diff would read as clean."""

    experiment_id: Identifier
    inference: InferenceMode
    diff: WorldRealityDiff
    coverage: Coverage | None


class RunView(StrictModel):
    inference: InferenceMode
    run_id: Identifier
    experiment_id: Identifier
    run: RunName
    artifact: Ref
    execution: ExecutionRecord | None
    result: WorldRunResult
    findings: list[WorldInvariantFinding] = Field(max_length=256)


class CapsuleInfo(StrictModel):
    """A capsule stored as a project artifact. ``artifact.id`` is the capsule id."""

    artifact: Ref
    world_version: PackVersion
    capsule_schema_version: Literal[1, 2]
    inference: InferenceMode | None  # null for an import: nothing vouches for its origin


class ReplayResult(StrictModel):
    """Recorded-action replay. It never calls a model, so ``model_calls`` is always 0."""

    world_version: PackVersion
    inference: InferenceMode | None  # null when the capsule isn't tied to a campaign
    run_status: RunStatus  # checked against the adapter; a forged status is refused
    reproduced: bool
    matches: dict[str, bool]
    checks: dict[str, bool]
    metrics: dict[str, Count]
    guarded: bool | None
    model_calls: Annotated[Literal[0], _exact_type(int)]


class CapsuleImport(StrictModel):
    capsule: CapsuleInfo
    replay: ReplayResult


class ApprovalView(StrictModel):
    """``action`` is the exact payload the digest binds, recomputed before it is shown."""

    request: ApprovalRequest
    inference: InferenceMode | None  # null if the campaign can't be read
    effective_status: ApprovalStatus  # "expired" once past expiry, even before it is marked
    action: dict[str, JsonValue] | None
    decision: ApprovalDecision | None


class DecisionResult(StrictModel):
    """The recorded decision. ``resumed`` is false if the campaign could not continue yet.

    The decision stands either way; ``resume_error`` says why, and the owner can retry
    with ``POST /v1/campaigns/{campaign_id}/resume``.
    """

    decision: ApprovalDecision
    approval: ApprovalRequest
    resumed: bool
    resume_error: ErrorResponse | None
