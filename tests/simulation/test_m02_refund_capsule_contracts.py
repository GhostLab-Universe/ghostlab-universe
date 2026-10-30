"""The stdlib v1 replay and the ``capsule_v1`` contract reject the same metadata.

Separate from test_m02_refund_capsule.py because these tests need Pydantic.
"""

import copy
import json

import pytest
from pydantic import ValidationError

from ghostlab.capsule import _digest, export_capsule, replay
from ghostlab.contracts import CapsuleV1
from ghostlab.core import Fixture, Scenario, experiment
from test_m02_refund_capsule import METADATA_MUTATIONS


@pytest.fixture
def payload(tmp_path):
    fixture = Fixture()
    path = tmp_path / "refund.json"
    export_capsule(path, fixture, Scenario(timeout_after_commit=True),
                   experiment(fixture)["faulted"])
    return json.loads(path.read_text(encoding="utf-8"))["payload"]


def test_a_valid_capsule_passes_both(payload, tmp_path):
    envelope = {"payload": payload, "sha256": _digest(payload)}
    CapsuleV1.model_validate(envelope)
    path = tmp_path / "valid.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    assert replay(path)["reproduced"]


@pytest.mark.parametrize("mutate", METADATA_MUTATIONS)
def test_stdlib_replay_and_the_contract_reject_the_same_metadata(payload, tmp_path, mutate):
    changed = copy.deepcopy(payload)
    mutate(changed)
    envelope = {"payload": changed, "sha256": _digest(changed)}
    path = tmp_path / "c.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ValueError):
        replay(path)
    with pytest.raises(ValidationError):
        CapsuleV1.model_validate(envelope)
