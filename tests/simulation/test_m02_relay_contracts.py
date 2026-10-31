"""M02 relay evidence conforms to the M00 ``ghostlab-contracts/1.1`` world contracts.

Separate from test_m02_relay.py because these tests need Pydantic; the relay engine
itself stays stdlib only.
"""

import copy
import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from ghostlab.contracts.world import (CapsuleV2, WorldInvariantFinding, WorldRealityDiff,
                                      WorldRunResult, WorldScenario, WorldToolInvocation)
from ghostlab.contracts.world_refund import capsule_digest
from ghostlab.worlds import relay_capsule
from ghostlab.worlds.relay import RelayFixture, scenario_json
from ghostlab.worlds.relay_run import experiment, findings
from test_m02_relay import METADATA_MUTATIONS

FIXTURE = RelayFixture()
GUARDED = RelayFixture(guarded=True)
TAKEOVER = "takeover_with_queued_confirmation"


@pytest.fixture(scope="module")
def result():
    return experiment(FIXTURE)


def cases(result):
    yield FIXTURE, scenario_json(), result["baseline"]["relay-local-success"]
    yield FIXTURE, scenario_json(), result["baseline"]["relay-lease-checked"]
    for case in result["faults"].values():
        yield FIXTURE, case["scenario"], case["faulted"]
        yield FIXTURE, case["scenario"], case["repaired"]
        yield GUARDED, case["scenario"], case["guarded"]
    for race in result["races"].values():
        lab = RelayFixture.from_json(race["fixture"])
        yield lab, race["scenario"], race["lab"]
        yield replace(lab, guarded=True), race["scenario"], race["guarded"]


def test_runs_scenarios_and_invocations_validate_unchanged(result):
    for _, data, recorded in cases(result):
        assert WorldScenario.model_validate(data).model_dump(mode="json") == data
        assert WorldRunResult.model_validate(recorded).model_dump(mode="json") == recorded
        for index, event in enumerate(recorded["trace"]):
            WorldToolInvocation.model_validate({
                "schema_version": 1, "invocation_id": f"inv-{index}", "run_id": "relay-run",
                "provider_call_id": None, "world_version": "service-relay/1",
                "tool": event["request"]["tool"], "arguments": event["request"]["arguments"]})


def test_diffs_and_findings_validate(result):
    diffs = [case["diff"] for case in result["faults"].values()]
    diffs += [race["diff"] for race in result["races"].values() if "diff" in race]
    for diff in diffs:
        assert WorldRealityDiff.model_validate(diff).model_dump(mode="json") == diff
    for index, (fixture, _, recorded) in enumerate(cases(result)):
        for finding in findings(fixture, recorded, f"relay-run-{index}",
                                "2026-10-30T00:00:00Z"):
            assert WorldInvariantFinding.model_validate(finding).model_dump(mode="json") == finding


def test_exported_capsules_validate_with_the_same_checksum(result, tmp_path):
    for index, (fixture, data, recorded) in enumerate(cases(result)):
        path = tmp_path / f"c{index}.json"
        relay_capsule.export_capsule(path, fixture, data, recorded)
        envelope = json.loads(path.read_text(encoding="utf-8"))
        assert CapsuleV2.model_validate(envelope).model_dump(mode="json") == envelope
        assert capsule_digest(envelope["payload"]) == envelope["sha256"]
        assert relay_capsule.capsule_digest(envelope["payload"]) == envelope["sha256"]


@pytest.mark.parametrize("mutate", METADATA_MUTATIONS)
def test_stdlib_replay_and_the_contract_reject_the_same_metadata(result, tmp_path, mutate):
    """The dependency-free loader stays in step with ``capsule_v2`` for metadata."""
    case = result["faults"][TAKEOVER]
    path = tmp_path / "c.json"
    relay_capsule.export_capsule(path, FIXTURE, case["scenario"], case["faulted"])
    payload = copy.deepcopy(json.loads(path.read_text(encoding="utf-8"))["payload"])
    mutate(payload)
    envelope = {"payload": payload, "sha256": capsule_digest(payload)}
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ValueError):
        relay_capsule.replay(path)
    with pytest.raises(ValidationError):
        CapsuleV2.model_validate(envelope)
