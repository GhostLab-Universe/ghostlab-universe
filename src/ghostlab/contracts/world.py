"""Generic world contracts (``ghostlab-contracts/1.1``), validated against world packs.

These are additive siblings of the refund v1 contracts. Refund v1 contracts,
fixtures and v1 capsules are unchanged and keep working; refund evidence maps
losslessly onto these types (``ghostlab.contracts.world_refund``).

Every name a world defines (tools, faults, invariants, adapters) is checked
against the registered pack for the record's ``world_version``. World state
stays opaque JSON here: its schema belongs to the pack implementation (M02).
"""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints, model_validator

from .campaign import CampaignRecord, CampaignSpec
from .common import ErrorInfo, TrustedScope
from .memory import MemoryQuery, MemoryRecord
from .primitives import (Identifier, MinorUnits, SchemaVersion, Sha256Hex, ShortText,
                         StrictModel, Text, UtcTimestamp, _exact_type, require_unique)
from .tools import ToolArg
from .world_packs import AdapterId, PackVersion, WorldName, check_arguments, pack

CapsuleV2Version = Annotated[Literal[2], _exact_type(int)]
AdapterVersion = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._/-]{0,99}$")]
EventId = Annotated[int, Field(ge=1, le=1_000)]
ProviderCallId = Annotated[str, StringConstraints(min_length=1, max_length=256)]


def _require(condition: bool, message: str):
    if not condition:
        raise ValueError(message)


# --- scenarios and proposals ---------------------------------------------------------

class FaultParameter(StrictModel):
    name: WorldName
    value: Annotated[str, StringConstraints(max_length=200)] | int | bool


class FaultInjection(StrictModel):
    fault_id: WorldName
    after_event: EventId | None  # None: the pack's default trigger point
    parameters: list[FaultParameter] = Field(max_length=8)


class WorldScenario(StrictModel):
    """A fault schedule for one world. Model-facing (inside proposals): no free dicts."""

    schema_version: SchemaVersion
    world_version: PackVersion
    faults: list[FaultInjection] = Field(max_length=8)
    max_tool_calls: int = Field(ge=1, le=1_000)

    @model_validator(mode="after")
    def _check(self):
        world = pack(self.world_version)
        require_unique([f.fault_id for f in self.faults], "fault ids")
        for injection in self.faults:
            spec = world.fault(injection.fault_id)
            _require(spec is not None, f"{injection.fault_id!r} is not a fault of "
                                       f"{self.world_version}")
            declared = {p.name: p.type for p in spec.parameters}
            given = [p.name for p in injection.parameters]
            require_unique(given, "fault parameters")
            _require(set(given) == set(declared),
                     f"{injection.fault_id} needs parameters {sorted(declared)}")
            for parameter in injection.parameters:
                kind = {"string": str, "integer": int, "boolean": bool}[declared[parameter.name]]
                _require(type(parameter.value) is kind,
                         f"{injection.fault_id}.{parameter.name} must be {declared[parameter.name]}")
        return self


class WorldExperimentProposal(StrictModel):
    """Explorer structured output for any registered world. Identity-free."""

    schema_version: SchemaVersion
    world_version: PackVersion
    hypothesis: Text
    adapter_id: AdapterId
    scenario: WorldScenario
    expected_signal: Text

    @model_validator(mode="after")
    def _check(self):
        _require(self.scenario.world_version == self.world_version,
                 "scenario is for a different world")
        _require(pack(self.world_version).adapter(self.adapter_id) is not None,
                 f"{self.adapter_id!r} is not an adapter of {self.world_version}")
        return self


# --- campaigns and scopes (siblings of refund-only v1 contracts) ------------------------

class WorldCampaignSpec(CampaignSpec):
    """``campaign_spec`` for any registered world. Still carries no identity."""

    world_version: PackVersion


class WorldCampaignRecord(CampaignRecord):
    spec: WorldCampaignSpec


class WorldTrustedScope(TrustedScope):
    """``trusted_scope`` whose tools come from a registered world pack."""

    world_version: PackVersion
    allowed_tools: list[WorldName] = Field(max_length=32)

    @model_validator(mode="after")
    def _check_world_tools(self):
        world = pack(self.world_version)
        unknown = [t for t in self.allowed_tools if world.tool(t) is None]
        _require(not unknown, f"{unknown} are not tools of {self.world_version}")
        return self


# --- tools ---------------------------------------------------------------------------

class WorldToolDefinition(StrictModel):
    """A tool offered to a target, exactly as the reviewed pack declares it."""

    world_version: PackVersion
    name: WorldName
    description: Text
    parameters: dict[str, JsonValue]

    @model_validator(mode="after")
    def _check(self):
        spec = pack(self.world_version).tool(self.name)
        _require(spec is not None, f"{self.name!r} is not a tool of {self.world_version}")
        _require(self.parameters == spec.parameters, "parameters differ from the pack")
        return self


class WorldToolInvocation(StrictModel):
    """A validated call the controller may dispatch into a world."""

    schema_version: SchemaVersion
    invocation_id: Identifier
    run_id: Identifier
    provider_call_id: ProviderCallId | None
    world_version: PackVersion
    tool: WorldName
    arguments: dict[str, JsonValue]

    @model_validator(mode="after")
    def _check(self):
        spec = pack(self.world_version).tool(self.tool)
        _require(spec is not None, f"{self.tool!r} is not a tool of {self.world_version}")
        problems = check_arguments(spec.parameters, self.arguments)
        _require(not problems, "; ".join(problems))
        return self


class WorldToolResult(StrictModel):
    schema_version: SchemaVersion
    invocation_id: Identifier
    provider_call_id: ProviderCallId | None
    event_id: EventId | None
    status: Literal["ok", "error"]
    output: dict[str, JsonValue] | None
    error: ErrorInfo | None

    @model_validator(mode="after")
    def _check(self):
        expected = (True, False) if self.status == "ok" else (False, True)
        _require((self.output is not None, self.error is not None) == expected,
                 "ok results carry output only; error results carry error only")
        return self


# --- run evidence --------------------------------------------------------------------

class WorldRecordedRequest(StrictModel):
    """What the target attempted, recorded even when the world rejected it."""

    tool: Annotated[str, StringConstraints(max_length=200)]
    arguments: dict[str, JsonValue]


class WorldOutcomeResult(StrictModel):
    result: dict[str, JsonValue]


class WorldOutcomeError(StrictModel):
    error: Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]
    message: Text


class WorldTraceEvent(StrictModel):
    event_id: EventId
    request: WorldRecordedRequest
    outcome: WorldOutcomeResult | WorldOutcomeError
    state: dict[str, JsonValue]  # opaque per-world snapshot; schema owned by the pack


class WorldRunResult(StrictModel):
    schema_version: SchemaVersion
    world_version: PackVersion
    adapter_id: AdapterId
    adapter_version: AdapterVersion
    actor_id: ToolArg
    status: Literal["completed", "budget_exhausted"]
    trace: list[WorldTraceEvent] = Field(max_length=1_000)
    final_state: dict[str, JsonValue]
    checks: dict[str, bool]

    @model_validator(mode="after")
    def _check(self):
        world = pack(self.world_version)
        _require(world.adapter(self.adapter_id) is not None,
                 f"{self.adapter_id!r} is not an adapter of {self.world_version}")
        _require(set(self.checks) == world.invariant_names,
                 f"checks must be exactly {sorted(world.invariant_names)}")
        _require([e.event_id for e in self.trace] == list(range(1, len(self.trace) + 1)),
                 "trace event ids must be 1..n in order")
        return self


class WorldDivergence(StrictModel):
    event_id: EventId
    baseline: WorldTraceEvent | None
    faulted: WorldTraceEvent | None


class WorldRealityDiff(StrictModel):
    schema_version: SchemaVersion
    world_version: PackVersion
    first_divergence: WorldDivergence | None
    baseline_metrics: dict[str, MinorUnits]  # world-defined totals, e.g. refunded_minor
    faulted_metrics: dict[str, MinorUnits]
    violated_invariants: list[WorldName] = Field(max_length=16)

    @model_validator(mode="after")
    def _check(self):
        unknown = set(self.violated_invariants) - pack(self.world_version).invariant_names
        _require(not unknown, f"{sorted(unknown)} are not invariants of {self.world_version}")
        require_unique(self.violated_invariants, "violated_invariants")
        return self


class WorldInvariantFinding(StrictModel):
    """A verifier verdict from world evidence, never model confidence."""

    schema_version: SchemaVersion
    finding_id: Identifier
    run_id: Identifier
    world_version: PackVersion
    invariant: WorldName
    verifier_version: Identifier
    verdict: Literal["passed", "failed"]
    evidence_event_ids: list[EventId] = Field(max_length=1_000)
    observed_value: int | None = Field(ge=0, le=10**15)
    limit_value: int | None = Field(ge=0, le=10**15)
    unit: ShortText | None
    recorded_at: UtcTimestamp

    @model_validator(mode="after")
    def _check(self):
        _require(self.invariant in pack(self.world_version).invariant_names,
                 f"{self.invariant!r} is not an invariant of {self.world_version}")
        require_unique(self.evidence_event_ids, "evidence_event_ids")
        _require(self.verdict == "passed" or bool(self.evidence_event_ids),
                 "A failed invariant must cite evidence events")
        _require((self.observed_value is None and self.limit_value is None) or
                 self.unit is not None, "values need a unit")
        return self


# --- capsule v2 ----------------------------------------------------------------------

class CapsulePayloadV2(StrictModel):
    """Failure Capsule for any registered world. v1 capsules stay valid and replayable."""

    schema_version: CapsuleV2Version
    world_version: PackVersion
    fixture: dict[str, JsonValue]
    scenario: WorldScenario
    actor_id: ToolArg
    source: ShortText
    seed: int = Field(ge=0, le=2**53 - 1)
    recorded_model_outputs: list[JsonValue] = Field(max_length=1_000)
    run: WorldRunResult

    @model_validator(mode="after")
    def _check(self):
        _require(self.scenario.world_version == self.world_version == self.run.world_version,
                 "scenario, run and capsule must name the same world")
        _require(self.run.actor_id == self.actor_id, "Recorded principal does not match capsule")
        _require(len(self.run.trace) <= self.scenario.max_tool_calls,
                 "Trace longer than the scenario's tool budget")
        _require(2 in pack(self.world_version).replay.capsule_schema_versions,
                 f"{self.world_version} does not replay v2 capsules")
        return self


class CapsuleV2(StrictModel):
    """v2 has no separate loader yet, so the contract itself verifies the checksum.

    The checksum detects accidental change; it does not establish authorship.
    """

    payload: CapsulePayloadV2
    sha256: Sha256Hex

    @model_validator(mode="after")
    def _check_checksum(self):
        payload = self.payload.model_dump(mode="json")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=True, allow_nan=False).encode())
        _require(digest.hexdigest() == self.sha256, "Capsule checksum mismatch")
        return self


# --- memory (siblings of refund-only v1 contracts) ------------------------------------

class WorldMemoryRecord(MemoryRecord):
    world_version: PackVersion


class WorldMemoryQuery(MemoryQuery):
    world_version: PackVersion | None
