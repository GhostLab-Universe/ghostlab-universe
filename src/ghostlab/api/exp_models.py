"""HTTP views for experiment configs (``ghostlab-api/1``).

Thin typed envelopes over the experiment store records: the validated
config, resolved and run bodies cross as plain JSON data. Project and
principal come from the session, never a request.
"""

from pydantic import Field, JsonValue

from ghostlab.contracts.primitives import StrictModel


class ExpCreateRequest(StrictModel):
    config: dict[str, JsonValue]


class ExpRequested(StrictModel):
    config_id: str
    project_id: str
    name: str
    config: dict[str, JsonValue]
    digest: str
    requested_by: str
    created_at: str


class ExpRequestedList(StrictModel):
    configs: list[ExpRequested]


class ExpResolveRequest(StrictModel):
    manifest_digest: str | None = None


class ExpResolved(StrictModel):
    resolved_id: str
    project_id: str
    requested_id: str
    resolved: dict[str, JsonValue]
    digest: str
    created_at: str


class ExpResolvedList(StrictModel):
    configs: list[ExpResolved]


class ExpRunRequest(StrictModel):
    repetition: int = Field(ge=1)
    attempt: int = Field(ge=1)


class ExpRun(StrictModel):
    run_id: str
    project_id: str
    resolved_id: str
    repetition: int
    attempt: int
    run: dict[str, JsonValue]
    digest: str
    created_at: str


class ExpRunList(StrictModel):
    runs: list[ExpRun]


class ExpDiffEntry(StrictModel):
    path: str
    left: JsonValue
    right: JsonValue


class ExpDiff(StrictModel):
    left_id: str | None
    right_id: str | None
    left_digest: str | None
    right_digest: str | None
    equal: bool
    diff_count: int
    truncated: bool
    diffs: list[ExpDiffEntry]


class ExpPolicy(StrictModel):
    policy_version: str
    ceiling_tool_calls: int
    ceiling_total_tool_calls: int
    ceiling_repetitions: int
    ceiling_wall_clock_ms: int
    allowed_targets: list[str]
    allowed_hooks: list[str]
