"""Reservation-world/1 v3 enveloping: manifest review plus envelope round-trip.

No __init__.py here, so the stdlib core suite still runs without Pydantic.

Every envelope below is built from EXECUTED evidence: the P6 demo agents
(``examples/p6_demo_agents.py``, dicts in/out, zero GhostLab imports) run
against the vendored reservation service through the reviewed adapter and the
frozen extension interfaces, exactly like ``scripts/measured_demo.py``. No
published example files are added: the contract registry and every published
schema stay byte-identical (adding or widening a registered model would force
a bundle regeneration that the pre-existing set-1 drift on the base commit
blocks; reader unification is the documented follow-up).
"""

import copy
import json
import sys

import pytest
from pydantic import ValidationError

from ghostlab import capsule as capsule_v1
from ghostlab.adapters import reservation_env
from ghostlab.adapters.reservation_env import (ENVIRONMENT_ID, ENVIRONMENT_VERSION,
                                               RESERVATION_WORLD_VERSION, SERVICE_PROVENANCE,
                                               VENDOR_SHA256, ReservationEnvironment,
                                               register_reservation_adapters)
from ghostlab.adapters.reservation_service import ReservationService
from ghostlab.contracts.capsule_v3 import (CapsuleChecksumError, CapsuleV3, ExternalDependency,
                                           capsule_digest, fixture_digest)
from ghostlab.contracts.export import ROOT
from ghostlab.contracts.world import CapsuleV2
from ghostlab.contracts.world_packs import check_arguments
from ghostlab.contracts.world_reservation import (
    DEFAULT_MAX_TOOL_CALLS,
    RESERVATION_ADAPTER_POLICY, RESERVATION_FAULTS, RESERVATION_FIXTURES,
    RESERVATION_INVARIANTS, RESERVATION_REPLAY, RESERVATION_SOURCE, RESERVATION_TOOLS,
    RESERVATION_WORLD, ReservationCapsule, ReservationCapsulePayload, ReservationRunResult,
    ReservationScenario, manifest_json, reservation_capsule_to_evidence,
    reservation_evidence_to_v3, reservation_fault, reservation_tool,
    run_from_reservation, run_to_reservation, scenario_from_reservation,
    scenario_to_reservation, vendored_service_dependency)
from ghostlab.extensions.environment import Action
from ghostlab.extensions.qualify import ScriptTarget
from ghostlab.extensions.runner import run_attempt
from ghostlab.extensions.target import TargetTask
from ghostlab.replay import allow_tools, check_verdicts, replay_recorded
from ghostlab.targets.external import ExternalTargetAdapter

sys.path.insert(0, str(ROOT / "examples"))
from p6_demo_agents import BlindBooker, CarefulBooker  # noqa: E402  (demo parity, like measured_demo.py)

WORLD = RESERVATION_WORLD_VERSION
FIXTURE = "near_full"
ACTOR = "demo-booker-task"
FULL_SCENARIO = {"timeout_after_commit": True, "stale_snapshot": True,
                 "skip_capacity_check": True, "max_tool_calls": 20}
CLEAN_SCENARIO = {"timeout_after_commit": False, "stale_snapshot": False,
                  "skip_capacity_check": False, "max_tool_calls": 20}
GATEWAY_TOOLS = ["check_availability", "reserve", "cancel", "get_status"]


def run_agent(agent, scenario, fixture=FIXTURE):
    """Run one demo agent exactly like scripts/measured_demo.py does."""
    registry = register_reservation_adapters()
    env = registry.environment_factory(WORLD)()
    snapshot = dict(env.named_fixture(fixture))
    env.reset(dict(snapshot), dict(scenario), actor_id=ACTOR)
    attempt = run_attempt(env, ExternalTargetAdapter(agent),
                          TargetTask(task_id="book-two-seats", params={}),
                          authorize=allow_tools(*GATEWAY_TOOLS), cap=20)
    evaluator = ReservationEnvironment()
    verdicts = dict(check_verdicts(evaluator.evaluate(trace=attempt.trace,
                                                      state=attempt.state)))
    fired = sorted(evaluator.faults_fired(dict(scenario), attempt.trace))
    return {"snapshot": snapshot, "scenario": dict(scenario), "attempt": attempt,
            "verdicts": verdicts, "fired": fired}


def run_script():
    """A fixed three-call script that fires every fault (read, overbook, stale read)."""
    registry = register_reservation_adapters()
    env = registry.environment_factory(WORLD)()
    snapshot = dict(env.named_fixture(FIXTURE))
    env.reset(dict(snapshot), dict(FULL_SCENARIO), actor_id=ACTOR)
    script = [Action(tool="check_availability", arguments={}),
              Action(tool="reserve", arguments={"holder": "demo", "seats": 2}),
              Action(tool="check_availability", arguments={})]
    attempt = run_attempt(env, ScriptTarget(list(script)),
                          TargetTask(task_id="scripted", params={}),
                          authorize=lambda action: None, cap=20, cooperative_stop=False)
    evaluator = ReservationEnvironment()
    verdicts = dict(check_verdicts(evaluator.evaluate(trace=attempt.trace,
                                                      state=attempt.state)))
    fired = sorted(evaluator.faults_fired(dict(FULL_SCENARIO), attempt.trace))
    return {"snapshot": snapshot, "scenario": dict(FULL_SCENARIO), "attempt": attempt,
            "verdicts": verdicts, "fired": fired}


def seal(run, agent=BlindBooker(), **overrides):
    """Seal executed evidence; overrides replace builder inputs by name."""
    inputs = {"fixture": run["snapshot"], "scenario": run["scenario"], "actor_id": ACTOR,
              "adapter_id": agent.agent_id, "adapter_version": agent.agent_version,
              "status": run["attempt"].status, "trace": list(run["attempt"].trace),
              "final_state": dict(run["attempt"].state), "checks": dict(run["verdicts"]),
              "faults_fired": list(run["fired"])}
    inputs.update(overrides)
    return reservation_evidence_to_v3(**inputs)


def rejects_reservation(payload) -> None:
    """Reject for a shape or rule reason, never for a stale test checksum."""
    with pytest.raises(ValidationError) as from_json:
        ReservationCapsule.model_validate_json(json.dumps(payload))
    with pytest.raises(ValidationError) as from_object:
        ReservationCapsule.model_validate(payload)
    assert "Capsule checksum mismatch" not in str(from_json.value)
    assert "Capsule checksum mismatch" not in str(from_object.value)


# --- the reviewed manifest mirrors the adapter and the service -------------------

def test_manifest_mirrors_the_reviewed_adapter():
    assert RESERVATION_WORLD == WORLD == "reservation-world/1"
    assert {name: spec["parameters"] for name, spec in RESERVATION_TOOLS.items()} == \
        reservation_env.RESERVATION_TOOL_SCHEMAS
    assert {name: spec["effect"] for name, spec in RESERVATION_TOOLS.items()} == \
        reservation_env.TOOL_EFFECTS
    assert {name: spec["description"] for name, spec in RESERVATION_TOOLS.items()} == \
        reservation_env.TOOL_DESCRIPTIONS
    assert tuple(RESERVATION_TOOLS) == reservation_env.ALLOWED_TOOLS
    assert (set(RESERVATION_FAULTS), reservation_env.FAULT_IDS) == \
        (set(reservation_env.FAULTS), reservation_env.FAULT_IDS)
    assert list(RESERVATION_FAULTS) == list(reservation_env.FAULT_IDS)  # schedule order
    for fault_id, spec in RESERVATION_FAULTS.items():
        assert tuple(spec["trigger_points"]) == \
            reservation_env.FAULT_TRIGGER_POINTS[fault_id]
        assert spec["description"] == reservation_env.FAULT_DESCRIPTIONS[fault_id]
    assert set(RESERVATION_INVARIANTS) == set(reservation_env.RESERVATION_INVARIANTS)
    assert RESERVATION_FIXTURES == reservation_env.RESERVATION_FIXTURES
    assert (reservation_env.ENVIRONMENT_ID, reservation_env.ENVIRONMENT_VERSION,
            reservation_env.SOURCE) == (ENVIRONMENT_ID, ENVIRONMENT_VERSION,
                                        RESERVATION_SOURCE)
    assert SERVICE_PROVENANCE == reservation_env.SERVICE_PROVENANCE
    assert (DEFAULT_MAX_TOOL_CALLS ==
            reservation_env.DEFAULT_MAX_TOOL_CALLS)
    assert VENDOR_SHA256.lower() == vendored_service_dependency()["digest"]
    assert reservation_tool("reserve")["effect"] == "mutate"
    assert reservation_tool("no_such_tool") is None
    assert reservation_fault("stale_snapshot")["trigger_points"] == ("check_availability",)
    assert reservation_fault("no_such_fault") is None


def test_manifest_tool_arguments_agree_with_the_service():
    service = ReservationService("half_full")
    assert check_arguments(RESERVATION_TOOLS["reserve"]["parameters"],
                           {"holder": "carol", "seats": 2}) == []
    assert service.call_tool("reserve", {"holder": "carol", "seats": 2})["ok"]
    assert check_arguments(RESERVATION_TOOLS["reserve"]["parameters"],
                           {"holder": "carol"}) == ["missing argument 'seats'"]
    assert check_arguments(RESERVATION_TOOLS["cancel"]["parameters"],
                           {"reservation_id": "R-0001", "extra": 1}) == [
        "unexpected argument 'extra'"]
    assert check_arguments(RESERVATION_TOOLS["check_availability"]["parameters"], {}) == []
    assert service.call_tool("reserve", {"holder": "carol"})["ok"] is False
    assert service.call_tool("cancel", {"reservation_id": "R-0001", "extra": 1})["ok"] is False


def test_manifest_fixtures_invariants_and_replay_are_pinned():
    manifest = manifest_json()
    assert manifest["world_version"] == WORLD
    assert manifest["fixtures"] == ["empty_small", "full", "half_full", "near_full"]
    assert sorted(manifest["invariants"]) == ["capacity_respected", "records_wellformed",
                                              "used_consistent", "used_nonnegative"]
    assert manifest["replay"] == RESERVATION_REPLAY == {"capsule_schema_versions": [3],
                                                        "dispatch": "recorded_actions"}
    assert manifest["adapter_policy"] == RESERVATION_ADAPTER_POLICY == "external"
    assert manifest["environment"] == {"id": ENVIRONMENT_ID,
                                       "version": ENVIRONMENT_VERSION}
    assert manifest["vendored_service"] == vendored_service_dependency()
    # The adapter evaluates exactly the manifest invariants on executed evidence.
    run = run_agent(BlindBooker(), FULL_SCENARIO)
    assert sorted(run["verdicts"]) == sorted(RESERVATION_INVARIANTS)


def test_vendored_service_dependency_is_pinned():
    dependency = vendored_service_dependency()
    assert ExternalDependency.model_validate(dependency).model_dump() == dependency
    assert dependency["name"] == "reservation-service"
    assert dependency["version"] == "ghostlab-ext-example@79684eb"
    assert SERVICE_PROVENANCE.startswith(dependency["version"])
    assert dependency["digest"] == VENDOR_SHA256.lower() != VENDOR_SHA256  # Sha256Hex is lowercase


# --- envelopes built from executed demo evidence -----------------------------------

def test_defective_demo_agent_violates_capacity_like_the_demo():
    run = run_agent(BlindBooker(), FULL_SCENARIO)
    failed = sorted(name for name, hit in run["verdicts"].items() if hit is False)
    assert failed == ["capacity_respected"]  # measured_demo.py expects the same violation
    assert run["fired"] == ["skip_capacity_check", "timeout_after_commit"]
    capsule = seal(run)
    assert capsule.payload.schema_version == 3
    assert capsule.sha256 == capsule_digest(capsule.payload.model_dump(mode="json"))
    assert capsule.payload.run.checks["capacity_respected"] is False
    provenance = capsule.payload.provenance
    assert (provenance.environment_id, provenance.environment_version,
            provenance.fixture_name) == (ENVIRONMENT_ID, ENVIRONMENT_VERSION, FIXTURE)
    assert provenance.fixture_sha256 == fixture_digest(run["snapshot"])
    assert list(provenance.faults_fired) == run["fired"]
    [dependency] = provenance.external_dependencies
    assert dependency.model_dump() == vendored_service_dependency()
    assert provenance.truncated is False and provenance.replay_kind == "recorded"


def test_fixed_demo_agent_passes_and_envelopes_clean():
    run = run_agent(CarefulBooker(), CLEAN_SCENARIO)
    assert run["verdicts"] == {name: True for name in RESERVATION_INVARIANTS}
    assert run["fired"] == []
    capsule = seal(run, agent=CarefulBooker())
    assert capsule.payload.run.adapter_id == "demo-booker"
    assert capsule.payload.run.adapter_version == "demo/2"
    assert capsule.payload.provenance.faults_fired == []


def test_evidence_round_trip_is_lossless():
    run = run_agent(BlindBooker(), FULL_SCENARIO)
    capsule = seal(run)
    first = capsule.model_dump(mode="json")
    evidence = reservation_capsule_to_evidence(copy.deepcopy(first))
    assert evidence["world_version"] == WORLD
    assert evidence["fixture"] == run["snapshot"]
    assert evidence["scenario"] == FULL_SCENARIO
    assert evidence["run"]["trace"] == run["attempt"].trace  # per-event actor restored
    assert evidence["run"]["final_state"] == run["attempt"].state
    assert evidence["run"]["checks"] == run["verdicts"]
    assert evidence["run"]["adapter"] == BlindBooker.agent_id
    resealed = seal(run).model_dump(mode="json")
    assert resealed == first  # deterministic sealing
    assert reservation_capsule_to_evidence(resealed) == evidence


def test_capsule_bytes_round_trip_exactly():
    capsule = seal(run_agent(BlindBooker(), FULL_SCENARIO))
    first = capsule.model_dump(mode="json")
    assert ReservationCapsule.model_validate_json(json.dumps(first)).model_dump(
        mode="json") == first  # identical, including the checksum


def test_recorded_evidence_replays_without_a_model():
    run = run_agent(BlindBooker(), FULL_SCENARIO)
    evidence = reservation_capsule_to_evidence(seal(run).model_dump(mode="json"))
    result = replay_recorded(
        registry=register_reservation_adapters(), world_version=WORLD,
        fixture=evidence["fixture"], scenario=evidence["scenario"],
        actor_id=evidence["actor_id"], recorded_trace=evidence["run"]["trace"],
        recorded_state=evidence["run"]["final_state"],
        recorded_checks=evidence["run"]["checks"])
    assert result.reproduced, result.to_dict()


def test_scenario_mapping_canonicalizes_like_reset():
    assert scenario_from_reservation(scenario_to_reservation({})) == CLEAN_SCENARIO
    assert scenario_from_reservation(scenario_to_reservation(FULL_SCENARIO)) == FULL_SCENARIO
    assert scenario_from_reservation(
        scenario_to_reservation({"skip_capacity_check": True})) == {
        **CLEAN_SCENARIO, "skip_capacity_check": True}
    with pytest.raises(ValueError, match="Unknown scenario keys"):
        scenario_to_reservation({"max_tool_calls": 5, "latency_ms": 30})
    with pytest.raises(ValueError, match="must be boolean"):
        scenario_to_reservation({"skip_capacity_check": 1})
    with pytest.raises(ValueError, match="between 1 and 1000"):
        scenario_to_reservation({"max_tool_calls": 0})
    with pytest.raises(ValueError, match="Invalid reservation scenario"):
        scenario_to_reservation(["not-a-mapping"])


def test_custom_fault_timing_validates_but_does_not_map():
    timed = {"schema_version": 1, "world_version": WORLD,
             "faults": [{"fault_id": "timeout_after_commit", "after_event": 2,
                         "parameters": []}],
             "max_tool_calls": 20}
    ReservationScenario.model_validate(timed)  # the envelope allows it ...
    with pytest.raises(ValueError, match="no custom fault timing"):
        scenario_from_reservation(timed)  # ... but reset toggles cannot express it


# --- checksums are verified before conversion, never silently replaced -------------

def test_scripted_run_fires_every_fault():
    run = run_script()
    assert [e["request"]["tool"] for e in run["attempt"].trace] == [
        "check_availability", "reserve", "check_availability"]
    assert run["fired"] == ["skip_capacity_check", "stale_snapshot", "timeout_after_commit"]
    assert run["verdicts"]["capacity_respected"] is False
    capsule = seal(run, adapter_id="scripted-probe", adapter_version="scripts/1")
    assert list(capsule.payload.provenance.faults_fired) == run["fired"]


def test_tampered_capsules_are_not_converted():
    run = run_script()
    capsule = seal(run, adapter_id="scripted-probe",
                   adapter_version="scripts/1").model_dump(mode="json")
    tampered = copy.deepcopy(capsule)
    tampered["payload"]["seed"] = 7  # payload changed, checksum not
    with pytest.raises((ValidationError, CapsuleChecksumError)):
        reservation_capsule_to_evidence(tampered)
    tampered_trace = copy.deepcopy(capsule)
    tampered_trace["payload"]["run"]["trace"][0]["request"]["arguments"] = {"holder": "mallory"}
    tampered_trace["payload"]["provenance"]["faults_fired"] = []
    with pytest.raises((ValidationError, CapsuleChecksumError)):
        reservation_capsule_to_evidence(tampered_trace)
    zeroed = copy.deepcopy(capsule)
    zeroed["sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="Capsule checksum mismatch"):
        ReservationCapsule.model_validate(zeroed)
    with pytest.raises((ValidationError, CapsuleChecksumError)):
        reservation_capsule_to_evidence(zeroed)


# --- rejection: names are checked against the reservation manifest ------------------

def test_reservation_envelope_rejects_manifest_violations():
    run = run_script()
    assert len(run["attempt"].trace) == 3  # budget and ordering cases need several events
    capsule = seal(run, adapter_id="scripted-probe",
                   adapter_version="scripts/1").model_dump(mode="json")

    def resealed(mutator):
        envelope = copy.deepcopy(capsule)
        mutator(envelope["payload"])
        envelope["sha256"] = capsule_digest(envelope["payload"])
        return envelope

    rejects_reservation(resealed(lambda p: p.update(world_version="refund-world/1")))
    rejects_reservation(resealed(lambda p: p.update(world_version="creator-release/1")))
    rejects_reservation(resealed(lambda p: p.update(actor_id="another-principal")))
    rejects_reservation(resealed(lambda p: p.update(schema_version=2)))
    rejects_reservation(resealed(lambda p: p["scenario"].update(max_tool_calls=1)))
    rejects_reservation(resealed(lambda p: p["run"]["trace"].reverse()))
    rejects_reservation(resealed(
        lambda p: p["scenario"]["faults"].append(
            {"fault_id": "timeout_after_commit", "after_event": None, "parameters": []})))
    rejects_reservation(resealed(
        lambda p: p["scenario"]["faults"][0].update(fault_id="stale_gossip")))
    rejects_reservation(resealed(
        lambda p: p["scenario"]["faults"][0].update(
            parameters=[{"name": "delay_events", "value": 3}])))
    rejects_reservation(resealed(lambda p: p["run"]["checks"].pop("capacity_respected")))
    rejects_reservation(resealed(
        lambda p: p["run"]["checks"].update(total_refund_within_payment=True)))
    rejects_reservation(resealed(lambda p: p["run"].update(adapter_id="Has-Caps")))
    rejects_reservation(resealed(lambda p: p["run"].update(adapter_version="bad version!")))
    rejects_reservation(resealed(
        lambda p: p["provenance"].update(fixture_sha256="0" * 64)))
    rejects_reservation(resealed(
        lambda p: p["provenance"].update(
            faults_fired=["timeout_after_commit", "stale_gossip"])))
    rejects_reservation(resealed(
        lambda p: p["provenance"].update(replay_kind="retest",
                                         retest_adapter_id="demo-booker")))
    envelope = copy.deepcopy(capsule)
    envelope["payload"]["provenance"]["external_dependencies"].append(
        vendored_service_dependency())
    envelope["sha256"] = capsule_digest(envelope["payload"])
    rejects_reservation(envelope)  # dependency names must be unique


def test_run_mapping_checks_the_recorded_principal():
    run = run_agent(BlindBooker(), FULL_SCENARIO)
    evidence = {"adapter": BlindBooker.agent_id, "adapter_version": BlindBooker.agent_version,
                "actor_id": ACTOR, "status": run["attempt"].status,
                "trace": run["attempt"].trace, "final_state": run["attempt"].state,
                "checks": run["verdicts"]}
    assert run_from_reservation(run_to_reservation(evidence))["trace"] == run["attempt"].trace
    forged = copy.deepcopy(evidence)
    forged["trace"][0]["actor_id"] = "someone-else"
    with pytest.raises(ValueError, match="Recorded principal"):
        run_to_reservation(forged)
    with pytest.raises(ValueError, match="missing"):
        run_to_reservation({"adapter": "demo-booker"})
    assert list(run_to_reservation({**evidence, "trace": []}).trace) == []


def test_builder_requires_detection_and_validates_shapes():
    run = run_agent(BlindBooker(), FULL_SCENARIO)
    with pytest.raises(TypeError):  # faults_fired has no default
        reservation_evidence_to_v3(fixture=run["snapshot"], scenario=run["scenario"],
                                   actor_id=ACTOR, adapter_id="demo-booker",
                                   adapter_version="demo/1", status="completed",
                                   trace=[], final_state={}, checks={})
    base = {"fixture": run["snapshot"], "scenario": run["scenario"], "actor_id": ACTOR,
            "adapter_id": "demo-booker", "adapter_version": "demo/1",
            "status": run["attempt"].status, "trace": list(run["attempt"].trace),
            "final_state": dict(run["attempt"].state), "checks": dict(run["verdicts"]),
            "faults_fired": list(run["fired"])}
    for key, bad in [("actor_id", ""), ("adapter_id", ""), ("status", ""),
                     ("trace", {}), ("final_state", []), ("checks", {"ok": "yes"}),
                     ("faults_fired", "skip_capacity_check"), ("source", ""),
                     ("seed", -1), ("recorded_model_outputs", {}),
                     ("external_dependencies", ["x"]),
                     ("rule_versions", []), ("config", 7)]:
        with pytest.raises(ValueError):
            reservation_evidence_to_v3(**{**base, key: bad})
    with pytest.raises(ValueError, match="'fixture' name"):
        reservation_evidence_to_v3(**{**base, "fixture": {"capacity": 6}})
    extra = {"name": "booking-policy", "version": "policy/3", "digest": None}
    capsule = reservation_evidence_to_v3(**{**base, "external_dependencies": [extra]})
    assert [dep.model_dump() for dep in capsule.payload.provenance.external_dependencies] == [
        vendored_service_dependency(), extra]


# --- the reservation envelope is separate from every pack reader --------------------

def test_pack_readers_reject_reservation_capsules(tmp_path):
    capsule = seal(run_agent(BlindBooker(), FULL_SCENARIO)).model_dump(mode="json")
    path = tmp_path / "capsule.json"
    path.write_text(json.dumps(capsule), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid capsule payload"):
        capsule_v1.load_capsule(path)
    with pytest.raises(ValidationError):
        CapsuleV2.model_validate(capsule)
    with pytest.raises(ValidationError):
        CapsuleV3.model_validate(capsule)


def test_reservation_reader_rejects_pack_capsules():
    examples = ROOT / "examples" / "contracts" / "core"
    for name in ("capsule_v1.refund_failure", "capsule_v2.refund_failure",
                 "capsule_v2.journey_fixture", "capsule_v3.refund_failure",
                 "capsule_v3.journey_fixture"):
        rejects_reservation(json.loads((examples / f"{name}.json").read_text("utf-8")))


def test_pack_worlds_do_not_map_to_reservation_evidence():
    examples = ROOT / "examples" / "contracts" / "core"
    journey_run = json.loads((examples / "world_run_result.journey_fixture.json")
                             .read_text("utf-8"))
    with pytest.raises((ValidationError, ValueError)):
        run_to_reservation({"adapter": journey_run["adapter_id"],
                            "adapter_version": journey_run["adapter_version"],
                            "actor_id": journey_run["actor_id"], "status": journey_run["status"],
                            "trace": journey_run["trace"],
                            "final_state": journey_run["final_state"],
                            "checks": journey_run["checks"]})
    with pytest.raises((ValidationError, ValueError)):
        scenario_to_reservation({"timeout_after_commit": True, "delay_events": 3,
                                 "max_tool_calls": 20})
