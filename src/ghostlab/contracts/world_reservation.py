"""Reviewed reservation manifest plus v3-shaped enveloping for ``reservation-world/1``.

This is the M00 follow-up the v3 contract anticipated: ``reservation-world/1``
is an external world (a vendored service driven by a checked-in adapter), not
a frozen pack, so ``CapsuleV2``/``CapsuleV3`` -- whose ``world_version`` is the
closed ``PackVersion`` set -- cannot name it. This module adapts it to
pack-style enveloping without touching any registered contract:

* a reviewed manifest (tools, faults, invariants, fixtures, replay dispatch)
  written as literals and cross-checked against
  :mod:`ghostlab.adapters.reservation_env` and the vendored service in tests,
  the way pack manifests are cross-checked against the real core;
* scenario/run envelope types that mirror ``WorldScenario``/``WorldRunResult``
  field for field but validate names against the reservation manifest;
* a v3-shaped envelope (same payload layout, same ``CapsuleProvenance``, same
  canonical checksum) with checksum-validated envelope/convert paths that
  follow the existing gates: the checksum is verified before anything is
  converted, and it is never silently replaced.

Deliberate differences from pack worlds, each documented in
``docs/capsule-v3.md``:

* Adapter ids are an open set (this is a bring-your-own-agent world), so only
  their shape is checked -- the same policy ``CapsuleProvenance`` already
  applies to ``retest_adapter_id``. Pack worlds keep their closed sets.
* Reservation faults take no parameters and the world has no fault timing, so
  the envelope requires empty parameter lists; a non-default ``after_event``
  validates in the envelope but is refused by the evidence mapping, exactly
  like the refund v1 mapping refuses custom timing.
* Every reservation capsule names the vendored service as an
  ``ExternalDependency`` with a pinned revision and digest. The service is
  reviewed in-tree code, but it is developed outside this tree and re-vendored
  by hash, so a capsule that omits the pin would overclaim self-containment.
  By the P5 rule this means the envelope itself does not claim self-contained
  recorded replay; ``replay_recorded`` still reproduces against byte-identical
  service code (proven in tests), and a replayer may verify the pinned digest.

No new contract version is minted: the payload layout, provenance rules and
checksum algorithm are v3's unchanged. Unifying the pack and reservation
readers into one ``CapsuleV3`` model is deferred until the pre-existing set-1
schema drift on the base commit is resolved by M00 (regenerating the contract
bundle would otherwise rewrite frozen set-1 pins).
"""

from collections.abc import Mapping
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from .capsule_v3 import (CapsuleChecksumError, CapsuleProvenance, CapsuleV3Version,
                         capsule_digest, fixture_digest)
from .primitives import SchemaVersion, Sha256Hex, ShortText, StrictModel, require_unique
from .tools import ToolArg
from .world import AdapterVersion, FaultInjection, WorldTraceEvent
from .world_packs import AdapterId

RESERVATION_WORLD = "reservation-world/1"
ReservationWorldVersion = Literal["reservation-world/1"]
"""The one non-pack world this milestone envelopes (no second one exists yet)."""

RESERVATION_ENVIRONMENT_ID = "reservation-environment"
RESERVATION_ENVIRONMENT_VERSION = "reservation-environment/1"
RESERVATION_SOURCE = ("ghostlab.adapters.reservation_env "
                      "(checked-in; drives the vendored reservation service)")
RESERVATION_SERVICE_PROVENANCE = "ghostlab-ext-example@79684eb reservation_service.py"
RESERVATION_SERVICE_REVISION = "ghostlab-ext-example@79684eb"
RESERVATION_SERVICE_DIGEST = "c54fb584d297a4b5d910b56add1c50fb6b12d53c34722493e42f89fbcb2d1464"
"""Lowercase form of the adapter's ``VENDOR_SHA256`` pin (``Sha256Hex`` shape)."""

RESERVATION_FIXTURES = ("empty_small", "full", "half_full", "near_full")

RESERVATION_TOOLS = {
    "check_availability": {
        "effect": "read",
        "parameters": {"type": "object", "additionalProperties": False, "required": [],
                       "properties": {}},
        "description": "Read a live capacity snapshot (used, available, revision)",
    },
    "reserve": {
        "effect": "mutate",
        "parameters": {"type": "object", "additionalProperties": False,
                       "required": ["holder", "seats"],
                       "properties": {
                           "holder": {"type": "string", "minLength": 1, "maxLength": 100},
                           "seats": {"type": "integer", "minimum": 1}}},
        "description": "Commit one reservation for a holder, within capacity",
    },
    "cancel": {
        "effect": "mutate",
        "parameters": {"type": "object", "additionalProperties": False,
                       "required": ["reservation_id"],
                       "properties": {"reservation_id": {"type": "string", "minLength": 1,
                                                        "maxLength": 100}}},
        "description": "Cancel an active reservation, freeing its seats",
    },
    "get_status": {
        "effect": "read",
        "parameters": {"type": "object", "additionalProperties": False,
                       "required": ["reservation_id"],
                       "properties": {"reservation_id": {"type": "string", "minLength": 1,
                                                        "maxLength": 100}}},
        "description": "Look up one reservation record by id",
    },
}
"""Tool schemas mirroring the reviewed adapter exactly (no invented bounds:
``seats`` has the service's minimum-only bound, unlike pack integer bounds)."""

RESERVATION_FAULTS = {
    "timeout_after_commit": {
        "trigger_points": ("reserve_commit",),
        "description": "A reserve commits, then its response times out",
    },
    "stale_snapshot": {
        "trigger_points": ("check_availability",),
        "description": "Availability reads return the last cached snapshot, flagged stale",
    },
    "skip_capacity_check": {
        "trigger_points": ("reserve_commit",),
        "description": "Reserve skips capacity enforcement (buggy commit)",
    },
}
"""Fault ids in schedule order with their P1 trigger points (no parameters)."""

RESERVATION_INVARIANTS = {
    "capacity_respected": "Committed seats never exceed capacity",
    "records_wellformed": "Every record has positive seats and a known status",
    "used_consistent": "Used seats equal the active reservations' seats",
    "used_nonnegative": "Used seats never go negative",
}

RESERVATION_REPLAY = {"capsule_schema_versions": [3], "dispatch": "recorded_actions"}
"""Reservation evidence replays through recorded actions and is v3-only:
there is no pack v2 capsule for a non-pack world."""

RESERVATION_ADAPTER_POLICY = "external"
"""Adapter ids are caller-supplied external agent ids (shape-checked only)."""

DEFAULT_MAX_TOOL_CALLS = 20


def _require(condition: bool, message: str):
    if not condition:
        raise ValueError(message)


def reservation_tool(name: str) -> dict | None:
    """The reviewed tool entry, or None when unknown (mirrors ``WorldPack.tool``)."""
    return RESERVATION_TOOLS.get(name)


def reservation_fault(fault_id: str) -> dict | None:
    """The reviewed fault entry, or None when unknown (mirrors ``WorldPack.fault``)."""
    return RESERVATION_FAULTS.get(fault_id)


def manifest_json() -> dict:
    """The reviewed reservation manifest as plain JSON data (for tests/docs)."""
    return {
        "world_version": RESERVATION_WORLD,
        "title": "Reservation world",
        "summary": ("A vendored external seat/inventory service: holders book and cancel "
                    "within capacity under timeout/stale-read/skip-check faults."),
        "tools": {name: {"effect": spec["effect"], "parameters": spec["parameters"],
                          "description": spec["description"]}
                  for name, spec in RESERVATION_TOOLS.items()},
        "faults": {fault_id: {"trigger_points": list(spec["trigger_points"]),
                               "description": spec["description"]}
                   for fault_id, spec in RESERVATION_FAULTS.items()},
        "invariants": dict(RESERVATION_INVARIANTS),
        "fixtures": list(RESERVATION_FIXTURES),
        "environment": {"id": RESERVATION_ENVIRONMENT_ID,
                        "version": RESERVATION_ENVIRONMENT_VERSION},
        "executable_via": "ghostlab.adapters.reservation_env (checked-in)",
        "adapter_policy": RESERVATION_ADAPTER_POLICY,
        "replay": {"capsule_schema_versions": list(
            RESERVATION_REPLAY["capsule_schema_versions"]),
            "dispatch": RESERVATION_REPLAY["dispatch"]},
        "vendored_service": vendored_service_dependency(),
        "source": RESERVATION_SOURCE,
    }


def vendored_service_dependency() -> dict:
    """The ``ExternalDependency`` payload for the vendored reservation service.

    Names plus pinned revision and digest only: no URIs, credentials or live
    handles. Returned as plain data so callers without the contracts extra can
    still read the pin; it validates under ``ExternalDependency``.
    """
    return {"name": "reservation-service", "version": RESERVATION_SERVICE_REVISION,
            "digest": RESERVATION_SERVICE_DIGEST}


# --- envelope scenario and run (mirrors of the generic world contracts) ------------

class ReservationScenario(StrictModel):
    """A fault schedule for ``reservation-world/1`` (same fields as ``WorldScenario``).

    Fault ids are checked against the reviewed reservation manifest instead of
    the pack registry. Reservation faults take no parameters, so the envelope
    requires empty parameter lists; a non-default ``after_event`` validates
    here but is refused by :func:`scenario_from_reservation`, exactly like the
    refund v1 mapping refuses custom fault timing.
    """

    schema_version: SchemaVersion
    world_version: ReservationWorldVersion
    faults: list[FaultInjection] = Field(max_length=8)
    max_tool_calls: int = Field(ge=1, le=1_000)

    @model_validator(mode="after")
    def _check(self):
        require_unique([f.fault_id for f in self.faults], "fault ids")
        for injection in self.faults:
            _require(reservation_fault(injection.fault_id) is not None,
                     f"{injection.fault_id!r} is not a fault of {RESERVATION_WORLD}")
            _require(injection.parameters == [],
                     f"{injection.fault_id} needs parameters []")
        return self


class ReservationRunResult(StrictModel):
    """A run record for ``reservation-world/1`` (same fields as ``WorldRunResult``).

    The checks must be exactly the manifest invariants and trace event ids
    must run 1..n, like the generic contract. The adapter id is an external
    agent id from an open set, so only its shape is checked.
    """

    schema_version: SchemaVersion
    world_version: ReservationWorldVersion
    adapter_id: AdapterId
    adapter_version: AdapterVersion
    actor_id: ToolArg
    status: Literal["completed", "budget_exhausted"]
    trace: list[WorldTraceEvent] = Field(max_length=1_000)
    final_state: dict[str, JsonValue]
    checks: dict[str, bool]

    @model_validator(mode="after")
    def _check(self):
        _require(set(self.checks) == set(RESERVATION_INVARIANTS),
                 f"checks must be exactly {sorted(RESERVATION_INVARIANTS)}")
        _require([e.event_id for e in self.trace] == list(range(1, len(self.trace) + 1)),
                 "trace event ids must be 1..n in order")
        return self


# --- the v3-shaped envelope ----------------------------------------------------

class ReservationCapsulePayload(StrictModel):
    """Failure Capsule for ``reservation-world/1``: the v3 layout plus provenance.

    Same field names, meanings and bounds as ``CapsulePayloadV3``; only the
    manifest behind ``world_version`` differs. ``schema_version`` stays 3: no
    new contract version is minted.
    """

    schema_version: CapsuleV3Version
    world_version: ReservationWorldVersion
    fixture: dict[str, JsonValue]
    scenario: ReservationScenario
    actor_id: ToolArg
    source: ShortText
    seed: int = Field(ge=0, le=2**53 - 1)
    recorded_model_outputs: list[JsonValue] = Field(max_length=1_000)
    run: ReservationRunResult
    provenance: CapsuleProvenance | None = None

    @model_validator(mode="after")
    def _check(self):
        _require(self.scenario.world_version == self.world_version == self.run.world_version,
                 "scenario, run and capsule must name the same world")
        _require(self.run.actor_id == self.actor_id, "Recorded principal does not match capsule")
        _require(len(self.run.trace) <= self.scenario.max_tool_calls,
                 "Trace longer than the scenario's tool budget")
        _require(3 in RESERVATION_REPLAY["capsule_schema_versions"],
                 f"{self.world_version} does not replay recorded-action capsules")
        provenance = self.provenance
        if provenance is not None:
            if provenance.fixture_sha256 is not None:
                _require(fixture_digest(self.fixture) == provenance.fixture_sha256,
                         "provenance.fixture_sha256 does not match the capsule fixture")
            scheduled = {fault.fault_id for fault in self.scenario.faults}
            unknown = [f for f in provenance.faults_fired if f not in scheduled]
            _require(not unknown, f"faults_fired {unknown} were not scheduled by the scenario")
        return self


class ReservationCapsule(StrictModel):
    """The sealed envelope: same checksum rule as every capsule version.

    The checksum detects accidental change; it does not establish authorship.
    """

    payload: ReservationCapsulePayload
    sha256: Sha256Hex

    @model_validator(mode="after")
    def _check_checksum(self):
        payload = self.payload.model_dump(mode="json")
        _require(capsule_digest(payload) == self.sha256, "Capsule checksum mismatch")
        return self


# --- lossless mapping between extension evidence and the envelope ------------------

def _dump(model) -> dict:
    return model.model_dump(mode="json")


def scenario_to_reservation(scenario: Mapping) -> ReservationScenario:
    """Extension toggle scenario (``reset`` shape) to an envelope scenario.

    Missing toggles default to False and a missing tool budget to 20, exactly
    like the adapter's ``reset`` parsing, so partial dicts canonicalize to the
    schedule the world would run. Unknown keys, non-boolean toggles and an
    out-of-range budget are refused.
    """
    if not isinstance(scenario, Mapping):
        raise ValueError("Invalid reservation scenario")
    unknown = set(scenario) - {*RESERVATION_FAULTS, "max_tool_calls"}
    if unknown:
        raise ValueError(f"Unknown scenario keys: {sorted(unknown, key=repr)}")
    faults = []
    for fault_id in RESERVATION_FAULTS:
        enabled = scenario.get(fault_id, False)
        if type(enabled) is not bool:
            raise ValueError(f"Scenario fault {fault_id!r} must be boolean")
        if enabled:
            faults.append({"fault_id": fault_id, "after_event": None, "parameters": []})
    cap = scenario.get("max_tool_calls", DEFAULT_MAX_TOOL_CALLS)
    if type(cap) is not int or not 1 <= cap <= 1000:
        raise ValueError("max_tool_calls must be between 1 and 1000")
    return ReservationScenario.model_validate(
        {"schema_version": 1, "world_version": RESERVATION_WORLD, "faults": faults,
         "max_tool_calls": cap})


def scenario_from_reservation(scenario: ReservationScenario | dict) -> dict:
    """Envelope scenario back to the canonical extension toggle scenario.

    Refuses custom fault timing: the world takes boolean toggles only, so an
    ``after_event`` would not survive a reset.
    """
    scenario = ReservationScenario.model_validate(scenario)
    if any(f.after_event is not None for f in scenario.faults):
        raise ValueError("reservation evidence has no custom fault timing")
    fired = {f.fault_id for f in scenario.faults}
    toggles = {fault_id: fault_id in fired for fault_id in RESERVATION_FAULTS}
    return {**toggles, "max_tool_calls": scenario.max_tool_calls}


def run_to_reservation(run: Mapping) -> ReservationRunResult:
    """Extension run evidence to an envelope run record.

    The input mirrors the generic run shape (``adapter``, ``adapter_version``,
    ``actor_id``, ``status``, ``trace``, ``final_state``, ``checks``). Trace
    events may carry the adapter's per-event ``actor_id``; when present it must
    equal the run's actor, and it is stripped, since the envelope records the
    principal once at the capsule level.
    """
    if not isinstance(run, Mapping):
        raise ValueError("Invalid reservation run")
    try:
        data = {"schema_version": 1, "world_version": RESERVATION_WORLD,
                "adapter_id": run["adapter"], "adapter_version": run["adapter_version"],
                "actor_id": run["actor_id"], "status": run["status"],
                "final_state": run["final_state"], "checks": run["checks"]}
        trace = run["trace"]
    except KeyError as error:
        raise ValueError(f"Invalid reservation run: missing {error}") from None
    if type(trace) is not list:
        raise ValueError("Invalid reservation run: trace must be a list")
    stripped = []
    for event in trace:
        if not isinstance(event, Mapping):
            raise ValueError("Invalid reservation run: trace events must be mappings")
        event = dict(event)
        actor = event.pop("actor_id", data["actor_id"])
        if actor != data["actor_id"]:
            raise ValueError("Recorded principal does not match capsule")
        stripped.append(event)
    return ReservationRunResult.model_validate({**data, "trace": stripped})


def run_from_reservation(run: ReservationRunResult | dict) -> dict:
    """Envelope run record back to extension run evidence.

    Re-attaches the run's actor to every trace event, restoring the adapter's
    recorded shape exactly.
    """
    data = _dump(ReservationRunResult.model_validate(run))
    trace = [{**event, "actor_id": data["actor_id"]} for event in data["trace"]]
    return {"adapter": data["adapter_id"], "adapter_version": data["adapter_version"],
            "actor_id": data["actor_id"], "status": data["status"], "trace": trace,
            "final_state": data["final_state"], "checks": data["checks"]}


def _verified_payload(capsule) -> dict:
    data = _dump(capsule)
    if capsule_digest(data["payload"]) != data["sha256"]:
        raise CapsuleChecksumError("Capsule checksum mismatch")
    return data["payload"]


def reservation_evidence_to_v3(*, fixture: Mapping, scenario: Mapping, actor_id: str,
                               adapter_id: str, adapter_version: str, status: str,
                               trace: list, final_state: Mapping, checks: Mapping,
                               faults_fired, rule_versions: Mapping | None = None,
                               config: Mapping | None = None,
                               external_dependencies=(),
                               truncated: bool = False, replay_kind: str = "recorded",
                               retest_adapter_id: str | None = None,
                               retest_adapter_version: str | None = None,
                               recorded_model_outputs=(),
                               seed: int = 0,
                               source: str = RESERVATION_SOURCE) -> ReservationCapsule:
    """Seal executed reservation evidence as a v3-shaped capsule.

    ``faults_fired`` has no default: it reports faults that demonstrably took
    effect (the environment's ``faults_fired`` detection), never the bare
    schedule. The vendored service dependency is always included first;
    ``external_dependencies`` carries any further resources the run needed.
    The fixture name is read from the snapshot's ``fixture`` key, and its
    digest is recomputed, so a mismatched snapshot fails validation.
    """
    if not isinstance(fixture, Mapping):
        raise ValueError("Invalid fixture")
    if not isinstance(scenario, Mapping):
        raise ValueError("Invalid scenario")
    if type(actor_id) is not str or not actor_id:
        raise ValueError("Invalid actor id")
    for value, label in ((adapter_id, "adapter id"), (adapter_version, "adapter version"),
                         (status, "status"), (replay_kind, "replay kind")):
        if type(value) is not str or not value:
            raise ValueError(f"Invalid {label}")
    if type(trace) is not list or len(trace) > 1000:
        raise ValueError("Invalid trace")
    if not isinstance(final_state, Mapping):
        raise ValueError("Invalid final state")
    if (type(checks) is not dict
            or any(type(name) is not str or type(hit) is not bool
                   for name, hit in checks.items())):
        raise ValueError("Invalid checks")
    if (not isinstance(faults_fired, (list, tuple))
            or any(type(name) is not str for name in faults_fired)):
        raise ValueError("Invalid faults fired")
    if type(source) is not str or not source:
        raise ValueError("Invalid source")
    if type(seed) is not int or not 0 <= seed <= 2**53 - 1:
        raise ValueError("Invalid seed")
    if not isinstance(recorded_model_outputs, (list, tuple)):
        raise ValueError("Invalid recorded model outputs")
    if not isinstance(external_dependencies, (list, tuple)) or any(
            not isinstance(dep, Mapping) for dep in external_dependencies):
        raise ValueError("Invalid external dependencies")
    for value, label in ((rule_versions, "rule versions"), (config, "config")):
        if value is not None and not isinstance(value, Mapping):
            raise ValueError(f"Invalid {label}")
    envelope_scenario = scenario_to_reservation(scenario)
    envelope_run = run_to_reservation({"adapter": adapter_id, "adapter_version": adapter_version,
                                       "actor_id": actor_id, "status": status, "trace": trace,
                                       "final_state": dict(final_state),
                                       "checks": dict(checks)})
    fixture = dict(fixture)
    name = fixture.get("fixture")
    if type(name) is not str or not name:
        raise ValueError("Fixture snapshot needs a 'fixture' name")
    provenance = {
        "replay_kind": replay_kind, "environment_id": RESERVATION_ENVIRONMENT_ID,
        "environment_version": RESERVATION_ENVIRONMENT_VERSION,
        "fixture_name": name, "fixture_sha256": fixture_digest(fixture),
        "rule_versions": dict(rule_versions or {}), "faults_fired": list(faults_fired),
        "config": dict(config or {}),
        "external_dependencies": [vendored_service_dependency(),
                                  *[dict(dep) for dep in external_dependencies]],
        "truncated": truncated, "retest_adapter_id": retest_adapter_id,
        "retest_adapter_version": retest_adapter_version,
    }
    payload = {"schema_version": 3, "world_version": RESERVATION_WORLD,
               "fixture": fixture, "scenario": _dump(envelope_scenario),
               "actor_id": actor_id, "source": source, "seed": seed,
               "recorded_model_outputs": list(recorded_model_outputs),
               "run": _dump(envelope_run), "provenance": provenance}
    return ReservationCapsule.model_validate({"payload": payload,
                                              "sha256": capsule_digest(payload)})


def reservation_capsule_to_evidence(capsule: ReservationCapsule | dict) -> dict:
    """Open a checksum-verified reservation capsule back into run evidence.

    The checksum is verified BEFORE anything is converted: a mismatch raises
    and is never replaced with a fresh-valid checksum.
    """
    payload = _verified_payload(ReservationCapsule.model_validate(capsule))
    return {"world_version": payload["world_version"], "fixture": payload["fixture"],
            "scenario": scenario_from_reservation(payload["scenario"]),
            "actor_id": payload["actor_id"], "source": payload["source"],
            "seed": payload["seed"],
            "recorded_model_outputs": payload["recorded_model_outputs"],
            "run": run_from_reservation(payload["run"]),
            "provenance": payload["provenance"]}


__all__ = [
    "DEFAULT_MAX_TOOL_CALLS", "RESERVATION_ADAPTER_POLICY", "RESERVATION_ENVIRONMENT_ID",
    "RESERVATION_ENVIRONMENT_VERSION", "RESERVATION_FAULTS", "RESERVATION_FIXTURES",
    "RESERVATION_INVARIANTS", "RESERVATION_REPLAY", "RESERVATION_SERVICE_DIGEST",
    "RESERVATION_SERVICE_PROVENANCE", "RESERVATION_SERVICE_REVISION", "RESERVATION_SOURCE",
    "RESERVATION_TOOLS", "RESERVATION_WORLD", "ReservationCapsule",
    "ReservationCapsulePayload", "ReservationRunResult", "ReservationScenario",
    "ReservationWorldVersion", "manifest_json", "reservation_capsule_to_evidence",
    "reservation_evidence_to_v3", "reservation_fault", "reservation_tool",
    "run_from_reservation", "run_to_reservation", "scenario_from_reservation",
    "scenario_to_reservation", "vendored_service_dependency",
]
