"""v1 refund capsule replay rejects metadata it cannot reproduce, and nothing else changes.

Stdlib and pytest only; test_m02_refund_capsule_contracts.py checks that the
``capsule_v1`` contract rejects the same capsules. No __init__.py here, so the
bare-interpreter core suite is unaffected.
"""

import json
from pathlib import Path

import pytest

from ghostlab.capsule import _digest, export_capsule, replay
from ghostlab.core import Fixture, Scenario, experiment, run

ROOT = Path(__file__).resolve().parents[2]
PUBLISHED = ROOT / "examples" / "contracts" / "core" / "capsule_v1.refund_failure.json"

# Each value is outside the capsule_v1 contract and is never reproduced by replay.
METADATA_MUTATIONS = [
    lambda p: p["run"].update(status="invented-invalid-status"),
    lambda p: p["run"].update(status=None),
    lambda p: p.update(seed=True),
    lambda p: p.update(seed=-1),
    lambda p: p.update(seed=2**53),
    lambda p: p.update(seed=1.5),
    lambda p: p.update(seed="0"),
    lambda p: p.update(recorded_model_outputs={}),
    lambda p: p.update(recorded_model_outputs=[None] * 1001),
    lambda p: p.update(source="uploaded-agent"),
    lambda p: p.update(source=""),
    lambda p: p["run"].update(adapter="uploaded-agent"),
    lambda p: p["run"].update(adapter_version="refund-adapters/2"),
]


def write(path, payload):
    path.write_text(json.dumps({"payload": payload, "sha256": _digest(payload)}),
                    encoding="utf-8")


@pytest.fixture
def exported(tmp_path):
    fixture = Fixture()
    path = tmp_path / "refund.json"
    export_capsule(path, fixture, Scenario(timeout_after_commit=True),
                   experiment(fixture)["faulted"])
    return path


@pytest.mark.parametrize("mutate", METADATA_MUTATIONS)
def test_invalid_metadata_with_a_fresh_checksum_is_rejected(exported, mutate):
    payload = json.loads(exported.read_text(encoding="utf-8"))["payload"]
    mutate(payload)
    write(exported, payload)
    with pytest.raises(ValueError):
        replay(exported)


def test_the_reported_combination_is_rejected(exported):
    payload = json.loads(exported.read_text(encoding="utf-8"))["payload"]
    payload["run"]["status"] = "invented-invalid-status"
    payload["seed"] = True
    payload["recorded_model_outputs"] = {}
    write(exported, payload)
    with pytest.raises(ValueError):
        replay(exported)


# --- valid capsules keep replaying exactly as before -----------------------------------

def test_the_published_v1_capsule_still_reproduces():
    result = replay(PUBLISHED)
    assert result["reproduced"] and result["model_calls"] == 0
    assert result["total_refunded_minor"] == 500_000


@pytest.mark.parametrize("scenario, adapter", [
    (Scenario(timeout_after_commit=True), "vulnerable"),
    (Scenario(timeout_after_commit=True), "repaired"),
    (Scenario(), "vulnerable"),
    (Scenario(max_tool_calls=1), "vulnerable"),  # budget_exhausted
])
def test_every_run_shape_still_reproduces(tmp_path, scenario, adapter):
    recorded = run(Fixture(), scenario, adapter)
    path = tmp_path / "c.json"
    export_capsule(path, Fixture(), scenario, recorded)
    assert replay(path)["reproduced"]


@pytest.mark.parametrize("change", [
    {"seed": 2**53 - 1}, {"seed": 0},
    {"recorded_model_outputs": [{"note": "unused"}, 1, "x", None]},
    {"recorded_model_outputs": [None] * 1000},
])
def test_contract_valid_metadata_is_still_accepted(exported, change):
    payload = json.loads(exported.read_text(encoding="utf-8"))["payload"]
    payload.update(change)
    write(exported, payload)
    assert replay(exported)["reproduced"]
