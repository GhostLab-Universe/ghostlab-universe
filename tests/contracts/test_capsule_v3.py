"""Capsule v3: old-capsule regression plus new-format round-trip and rejection.

No __init__.py here, so the stdlib core suite still runs without Pydantic.
"""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from ghostlab import capsule as capsule_v1
from ghostlab.contracts import world_refund as refund
from ghostlab.contracts.capsule_v3 import CapsuleChecksumError, CapsuleV3
from ghostlab.contracts.capsule_v3 import capsule_digest as v3_digest
from ghostlab.contracts.capsule_v3 import (capsule_v2_to_v3, capsule_v3_to_v2,
                                           fixture_digest)
from ghostlab.contracts.export import ROOT
from ghostlab.contracts.world import CapsuleV2
from ghostlab.worlds import journey_capsule

EXAMPLES = ROOT / "examples" / "contracts" / "core"



def ex(name: str) -> dict:
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# sha256 of the frozen artifacts, recorded at the v3 branch point.
FROZEN_ARTIFACTS = {
    "schemas/v1/capsule_v1.schema.json":
        "305268a4348331a810f661bc665b7729e82c466de20e6a305b1f1c28c53dd468",
    "schemas/v1/capsule_v2.schema.json":
        "8ae8554ea2f4454345d5cecb95a73ec2f441cdadf36b7698a5353bac6da1502d",
    "examples/contracts/core/capsule_v1.refund_failure.json":
        "848865e525ce96b99cf16b7226f3b1aff6bae4dea7c9bdb9579aafa7607a79fb",
    "examples/contracts/core/capsule_v2.refund_failure.json":
        "00fa2eb977cfa61a603da340d3ff650a916140707e58f3edf311d80123adb551",
    "examples/contracts/core/capsule_v2.journey_fixture.json":
        "56221646cb0147f883f1a5eb4d4a00b45d187402929aa905136385dee9c48760",
}


def v3ex(name: str) -> dict:
    return json.loads((EXAMPLES / f"capsule_v3.{name}.json").read_text(encoding="utf-8"))


def rejects_v3(payload) -> None:
    """Reject for a shape or rule reason, never for a stale test checksum."""
    with pytest.raises(ValidationError) as from_json:
        CapsuleV3.model_validate_json(json.dumps(payload))
    with pytest.raises(ValidationError) as from_object:
        CapsuleV3.model_validate(payload)
    assert "Capsule checksum mismatch" not in str(from_json.value)
    assert "Capsule checksum mismatch" not in str(from_object.value)


# --- v1 and v2 are preserved byte-for-byte ------------------------------------


def test_frozen_capsule_artifacts_are_byte_identical():
    for relative, frozen in FROZEN_ARTIFACTS.items():
        assert sha256_of(ROOT / relative) == frozen, relative


def test_v1_capsule_still_loads_and_replays(tmp_path):
    path = tmp_path / "capsule.json"
    data = ex("capsule_v1.refund_failure")
    path.write_text(json.dumps(data), encoding="utf-8")
    capsule_v1.load_capsule(path)
    report = capsule_v1.replay(path)
    assert report["reproduced"] and report["model_calls"] == 0
    assert report["total_refunded_minor"] == 500_000


def test_v2_capsules_still_validate_and_replay(tmp_path):
    CapsuleV2.model_validate(ex("capsule_v2.refund_failure"))
    CapsuleV2.model_validate(ex("capsule_v2.journey_fixture"))
    path = tmp_path / "capsule.json"
    path.write_text(json.dumps(ex("capsule_v2.journey_fixture")), encoding="utf-8")
    report = journey_capsule.replay(path)
    assert report["reproduced"] and report["model_calls"] == 0


# --- new-format round trips ---------------------------------------------------


def test_v1_through_v2_and_v3_chain_is_exact(tmp_path):
    original = ex("capsule_v1.refund_failure")
    up = capsule_v2_to_v3(refund.capsule_v1_to_v2(original))
    assert up.payload.schema_version == 3 and up.payload.provenance is None
    down = refund.capsule_v2_to_v1(capsule_v3_to_v2(up))
    assert down.model_dump(mode="json") == original
    path = tmp_path / "capsule.json"
    path.write_text(down.model_dump_json(indent=2), encoding="utf-8")
    report = capsule_v1.replay(path)
    assert report["reproduced"] and report["model_calls"] == 0


@pytest.mark.parametrize("name", ["capsule_v2.refund_failure",
                                  "capsule_v2.journey_fixture"])
def test_v2_to_v3_to_v2_is_exact(name):
    original = ex(name)
    up = capsule_v2_to_v3(original)
    assert up.payload.provenance is None
    assert up.sha256 == v3_digest(up.payload.model_dump(mode="json"))
    back = capsule_v3_to_v2(up)
    assert back.model_dump(mode="json") == original


def test_v3_examples_validate_and_downgrade():
    for name in ("refund_failure", "journey_fixture"):
        envelope = v3ex(name)
        capsule = CapsuleV3.model_validate(envelope)
        assert capsule.payload.schema_version == 3
        assert capsule_v3_to_v2(capsule).model_dump(mode="json") == ex(
            f"capsule_v2.{name}")


def _provenance(name):
    return copy.deepcopy(v3ex(name)["payload"]["provenance"])


def test_v3_to_v2_refuses_provenance_bearing_capsules():
    for name in ("refund_provenance", "refund_retest"):
        with pytest.raises(ValueError, match="refusing to drop"):
            capsule_v3_to_v2(v3ex(name))
    assert _provenance("refund_provenance")["replay_kind"] == "recorded"
    assert _provenance("refund_retest")["replay_kind"] == "retest"


def test_v3_to_v2_refuses_external_and_truncated_capsules():
    base = copy.deepcopy(v3ex("refund_failure"))
    payload = base["payload"]
    payload["provenance"] = {"replay_kind": "recorded", "environment_id": None,
                             "environment_version": None, "fixture_name": None,
                             "fixture_sha256": None, "rule_versions": {},
                             "faults_fired": [], "config": {},
                             "external_dependencies": [{"name": "ledger-api",
                                                        "version": None,
                                                        "digest": None}],
                             "truncated": True, "retest_adapter_id": None,
                             "retest_adapter_version": None}
    capsule = CapsuleV3.model_validate(
        {"payload": payload, "sha256": v3_digest(payload)})
    assert capsule.payload.provenance.truncated
    with pytest.raises(ValueError, match="external=1"):  # names what is missing
        capsule_v3_to_v2(capsule)


def test_checksums_are_validated_before_conversion():
    tampered = copy.deepcopy(ex("capsule_v2.refund_failure"))
    tampered["payload"]["seed"] = 7
    with pytest.raises((ValidationError, CapsuleChecksumError)):
        capsule_v2_to_v3(tampered)
    tampered3 = copy.deepcopy(v3ex("refund_failure"))
    tampered3["payload"]["seed"] = 7
    with pytest.raises((ValidationError, CapsuleChecksumError)):
        capsule_v3_to_v2(tampered3)
    zeroed = copy.deepcopy(v3ex("refund_failure"))
    zeroed["sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="Capsule checksum mismatch"):
        CapsuleV3.model_validate(zeroed)
    bad_sum = {**v3ex("refund_failure"), "sha256": "0" * 64}
    with pytest.raises(ValidationError, match="Capsule checksum mismatch"):
        CapsuleV3.model_validate(bad_sum)


# --- rejection: the v3 core keeps the v2 shape rules --------------------------


def test_v3_rejects_a_core_that_breaks_v2_shape_rules():
    original = v3ex("refund_failure")
    def resealed(mutator):
        envelope = copy.deepcopy(original)
        mutator(envelope["payload"])
        envelope["sha256"] = v3_digest(envelope["payload"])
        return envelope

    rejects_v3(resealed(lambda p: p.update(world_version="group-journey/1")))
    rejects_v3(resealed(lambda p: p.update(actor_id="another-principal")))
    rejects_v3(resealed(lambda p: p["scenario"].update(max_tool_calls=1)))
    rejects_v3(resealed(lambda p: p["run"]["trace"].reverse()))
    rejects_v3(resealed(lambda p: p.update(schema_version=2)))


def _provenance_envelope(mutator):
    envelope = copy.deepcopy(v3ex("refund_provenance"))
    mutator(envelope["payload"]["provenance"])
    envelope["sha256"] = v3_digest(envelope["payload"])
    return envelope


def test_provenance_names_a_retest_adapter_exactly_for_retests():
    CapsuleV3.model_validate(v3ex("refund_retest"))
    rejects_v3(_provenance_envelope(
        lambda p: p.update(retest_adapter_id="vulnerable")))
    only_version = _provenance_envelope(
        lambda p: p.update(replay_kind="retest",
                            retest_adapter_version="refund-adapters/1"))
    rejects_v3(only_version)  # retest needs both adapter fields
    only_id = _provenance_envelope(
        lambda p: p.update(replay_kind="retest", retest_adapter_id="vulnerable"))
    rejects_v3(only_id)


def test_provenance_fixture_digest_must_match_the_fixture():
    assert CapsuleV3.model_validate(v3ex("refund_provenance"))
    wrong = _provenance_envelope(
        lambda p: p.update(fixture_sha256="0" * 64))
    rejects_v3(wrong)
    assert fixture_digest(
        v3ex("refund_provenance")["payload"]["fixture"]) == v3ex(
        "refund_provenance")["payload"]["provenance"]["fixture_sha256"]


def test_provenance_faults_fired_must_be_scheduled():
    bad = _provenance_envelope(
        lambda p: p.update(faults_fired=["timeout_after_commit", "stale_gossip"]))
    rejects_v3(bad)



def test_provenance_bounds_are_rejected():
    too_many_rules = _provenance_envelope(
        lambda p: p.update(rule_versions={f"rule_{i}": "1" for i in range(17)}))
    rejects_v3(too_many_rules)
    dup_deps = _provenance_envelope(
        lambda p: p.update(external_dependencies=[{"name": "svc", "version": None, "digest": None}, {"name": "svc", "version": None, "digest": None}]))
    rejects_v3(dup_deps)
    bad_name = _provenance_envelope(
        lambda p: p.update(environment_id="Has-Caps"))
    rejects_v3(bad_name)
    bad_config = _provenance_envelope(
        lambda p: p.update(config={"sizes": {"nested": True}}))
    rejects_v3(bad_config)
    bad_flag = _provenance_envelope(lambda p: p.update(truncated=1))
    rejects_v3(bad_flag)


# --- version separation ---------------------------------------------------------


def test_v1_and_v2_readers_reject_v3_capsules(tmp_path):
    path = tmp_path / "capsule.json"
    path.write_text(json.dumps(v3ex("refund_failure")), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid capsule payload"):
        capsule_v1.load_capsule(path)
    with pytest.raises(ValueError, match="Invalid capsule payload"):
        journey_capsule.load_capsule(path)
    with pytest.raises(ValidationError):
        CapsuleV2.model_validate(v3ex("refund_failure"))


def test_v3_reader_rejects_v1_and_v2_capsules():
    rejects_v3(ex("capsule_v1.refund_failure"))
    rejects_v3(ex("capsule_v2.refund_failure"))


# --- the published provenance uses frozen P1 names --------------------------------


def test_provenance_uses_frozen_p1_names():
    from ghostlab.adapters.refund_env import (ENVIRONMENT_ID, ENVIRONMENT_VERSION,
                                              FIXTURE_NAME, REFUND_INVARIANTS,
                                              RefundEnvironment, TIMEOUT_FAULT)
    from ghostlab.orchestration.engine import VERIFIER_VERSION
    provenance = v3ex("refund_provenance")["payload"]["provenance"]
    assert (provenance["environment_id"], provenance["environment_version"],
            provenance["fixture_name"]) == (ENVIRONMENT_ID, ENVIRONMENT_VERSION,
                                         FIXTURE_NAME)
    payload = v3ex("refund_provenance")["payload"]
    assert provenance["fixture_sha256"] == fixture_digest(payload["fixture"])
    assert provenance["rule_versions"] == {name: VERIFIER_VERSION
                                          for name in REFUND_INVARIANTS}
    fired = RefundEnvironment().faults_fired(payload["scenario"], payload["run"]["trace"])
    assert provenance["faults_fired"] == sorted(fired) == [TIMEOUT_FAULT]
    assert provenance["external_dependencies"] == []
    assert provenance["truncated"] is False


def test_retest_example_matches_a_fresh_adapter_execution():
    from ghostlab.core import Fixture, Scenario, run
    fresh = run(Fixture(), Scenario(timeout_after_commit=True), "vulnerable")
    recorded = v3ex("refund_retest")["payload"]["run"]
    assert (recorded["status"], recorded["trace"]) == (fresh["status"], fresh["trace"])
    assert recorded["final_state"] == fresh["final_state"]
    provenance = v3ex("refund_retest")["payload"]["provenance"]
    assert provenance["retest_adapter_id"] == fresh["adapter"]
    assert provenance["retest_adapter_version"] == fresh["adapter_version"]
