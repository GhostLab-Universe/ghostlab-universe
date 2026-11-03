"""HTTP views for extension environments (``ghostlab-api/1``).

Thin typed summaries over the P2/P4/P5 reports: each response carries the
badge scalars the UI lists plus the full report under ``details``. Worlds
served here are synthetic local fixtures; probe actor ids are
caller-declared test identities, documented on the plane.
"""

from typing import Literal

from pydantic import Field, JsonValue, model_validator

from ghostlab.contracts.primitives import StrictModel


class ExtToolSpec(StrictModel):
    name: str
    effect: str
    parameters: dict[str, JsonValue]
    description: str


class ExtFaultParameter(StrictModel):
    name: str
    type: str
    description: str


class ExtFaultSpec(StrictModel):
    fault_id: str
    parameters: list[ExtFaultParameter]
    trigger_points: list[str]
    description: str


class ExtEnvironmentSummary(StrictModel):
    world_version: str
    environment_id: str
    environment_version: str
    source: str
    tools: list[str]
    faults: list[str]
    invariants: list[str]
    fixtures: list[str]
    capabilities: dict[str, bool]
    targets: list[str]


class ExtEnvironmentDetail(ExtEnvironmentSummary):
    tool_specs: list[ExtToolSpec]
    fault_specs: list[ExtFaultSpec]
    package: str
    package_version: str


class ExtFixture(StrictModel):
    world_version: str
    name: str
    snapshot: dict[str, JsonValue]


class ExtEnvironmentList(StrictModel):
    environments: list[ExtEnvironmentSummary]


class ExtAction(StrictModel):
    tool: str = Field(max_length=100)
    arguments: dict[str, JsonValue]


class ExtTask(StrictModel):
    task_id: str = Field(max_length=200)
    params: dict[str, JsonValue]


class ExtTargetRef(StrictModel):
    """A registered target id or an inline deterministic script."""

    kind: Literal["registered", "script"]
    id: str | None = None
    actions: list[ExtAction] | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.kind == "registered" and (not self.id or self.actions is not None):
            raise ValueError("registered targets name an id and no script")
        if self.kind == "script" and (
                self.id is not None or not self.actions or len(self.actions) > 100):
            raise ValueError("script targets carry 1 to 100 actions and no id")
        return self


class QualifyRequest(StrictModel):
    world_version: str
    fixture: str
    scenario: dict[str, JsonValue]
    script: list[ExtAction] = Field(min_length=1, max_length=100)
    task_id: str = "api-probe"


class ExtQualificationCheck(StrictModel):
    name: str
    passed: bool
    details: str


class ExtQualificationReport(StrictModel):
    world_version: str
    environment_id: str
    passed: bool
    checks: list[ExtQualificationCheck]
    missing_capabilities: list[str]
    notes: list[str]
    details: dict[str, JsonValue]


class FidelityRequest(StrictModel):
    world_version: str
    fixture: str
    scenario: dict[str, JsonValue]
    actor_id: str
    script: list[ExtAction] = Field(min_length=1, max_length=100)
    repeats: int = Field(default=3, ge=1, le=5)
    expected_trace: list[JsonValue] | None = None
    expected_state: dict[str, JsonValue] | None = None
    declared_tested: list[str] = Field(default_factory=list, max_length=32)
    declared_approximations: list[str] = Field(default_factory=list, max_length=32)
    declared_omissions: list[str] = Field(default_factory=list, max_length=32)


class ExtFidelityReport(StrictModel):
    world_version: str
    passed: bool
    determinism_identical: bool
    replay_reproduced: bool
    snapshot_supported: bool | None
    missing_capabilities: list[str]
    notes: list[str]
    details: dict[str, JsonValue]


class ExtCampaignAttempt(StrictModel):
    name: str = Field(max_length=200)
    arm: Literal["fixed-scenario", "seeded-random", "guided"]
    fixture_name: str
    scenario: dict[str, JsonValue]
    actor_id: str
    task: ExtTask
    target: ExtTargetRef
    cap: int = Field(ge=1, le=200)


class CampaignRequest(StrictModel):
    world_version: str
    attempts: list[ExtCampaignAttempt] = Field(min_length=1, max_length=20)
    allow_tools: list[str] | None = None


class ExtCampaignReport(StrictModel):
    attempts_run: int
    tools_used: list[str]
    faults_fired: list[str]
    rules_failed: list[str]
    caps_respected: bool
    guided_excluded: bool
    details: dict[str, JsonValue]


class InspectRequest(StrictModel):
    envelope: dict[str, JsonValue]


class ExtInspection(StrictModel):
    schema_version: int
    world_version: str
    replay_kind: str
    truncated: bool
    failed_invariants: list[str]
    external: list[str]
    replay_supported: bool
    replay_detail: str
    details: dict[str, JsonValue]


class ReplayRequest(StrictModel):
    world_version: str
    fixture: dict[str, JsonValue]
    scenario: dict[str, JsonValue]
    actor_id: str
    trace: list[JsonValue] = Field(max_length=1000)
    state: dict[str, JsonValue]
    checks: dict[str, bool]


class ExtReplayResult(StrictModel):
    reproduced: bool
    matches: dict[str, bool]
    divergence: dict[str, JsonValue] | None
    tool_calls: int
    failed: list[str]
    witness_sha256: str
    details: dict[str, JsonValue]


class RetestRequest(StrictModel):
    world_version: str
    target: ExtTargetRef
    fixture: dict[str, JsonValue]
    scenario: dict[str, JsonValue]
    actor_id: str
    task: ExtTask
    allow_tools: list[str] | None = None
    cap: int = Field(default=20, ge=1, le=200)


class ExtRetestResult(StrictModel):
    status: str
    tool_calls: int
    stopped: str | None
    denied: bool
    denial_summary: str | None
    target_adapter_id: str
    target_adapter_version: str
    failed: list[str]
    witness_sha256: str
    replay_equivalence_claimed: Literal[False]
    details: dict[str, JsonValue]


class MinimizeRequest(StrictModel):
    world_version: str
    target: ExtTargetRef
    fixture: dict[str, JsonValue]
    scenario_template: dict[str, JsonValue]
    fault_keys: list[str] = Field(min_length=1, max_length=8)
    actor_id: str
    task: ExtTask
    allow_tools: list[str] | None = None
    cap: int = Field(default=20, ge=1, le=200)
    invariant: str
    max_runs: int = Field(default=16, ge=1, le=16)
    deadline_ms: int = Field(default=5000, ge=1, le=30000)


class ExtMinimizeResult(StrictModel):
    status: str
    selected_faults: list[str] | None
    removed_faults: list[str]
    runs_used: int
    cardinality_minimal: bool
    witness_sha256: str
    regression: dict[str, JsonValue]
    details: dict[str, JsonValue]
