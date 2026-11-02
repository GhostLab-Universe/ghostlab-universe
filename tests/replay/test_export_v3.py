"""P5 export: v3 envelopes from extension runs, then replayed from the file."""

import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from ghostlab.adapters import register_refund_adapters
from ghostlab.contracts.capsule_v3 import CapsuleV3, capsule_digest, capsule_v2_to_v3
from ghostlab.contracts.export import ROOT
from ghostlab.contracts.world_refund import scenario_from_world
from ghostlab.core import Fixture
from ghostlab.extensions.runner import run_attempt
from ghostlab.extensions.target import TargetTask
from ghostlab.replay.export_v3 import export_refund_run_v3, export_v3, write_capsule_file
from ghostlab.replay.inspect import inspect_capsule, inspect_capsule_file
from ghostlab.replay.recorded import check_verdicts, replay_recorded

REFUND = "refund-world/1"
EXAMPLES = ROOT / "examples" / "contracts" / "core"


def failing_run():
    registry = register_refund_adapters()
    fixture = Fixture()
    scenario = {"timeout_after_commit": True, "max_tool_calls": 20}
    env = registry.environment_factory(REFUND)()
    target = registry.target_factory(REFUND, "vulnerable")()
    env.reset(asdict(fixture), dict(scenario), actor_id=fixture.customer_id)
    task = TargetTask(task_id="p5", params={"order_id": fixture.order_id,
                                            "customer_id": fixture.customer_id})
    attempt = run_attempt(env, target, task, authorize=lambda action: None, cap=20,
                          cooperative_stop=False)
    evaluator = registry.environment_factory(REFUND)()
    try:
        checks = evaluator.evaluate(trace=attempt.trace, state=attempt.state)
    finally:
        evaluator.close()
    fired = registry.environment_factory(REFUND)().faults_fired(
        scenario, attempt.trace)
    return registry, fixture, scenario, attempt, checks, fired


class ExportTest(unittest.TestCase):
    def test_export_replay_proof_in_clean_setup(self):
        registry, fixture, scenario, attempt, checks, fired = failing_run()
        verdicts = check_verdicts(checks)
        self.assertIn(False, verdicts.values())
        capsule = export_refund_run_v3(
            fixture=asdict(fixture), timeout_after_commit=True, max_tool_calls=20,
            actor_id=fixture.customer_id, adapter="vulnerable",
            adapter_version=target_version(registry), status=attempt.status,
            trace=attempt.trace, final_state=attempt.state, checks=verdicts,
            faults_fired=sorted(fired))
        with tempfile.TemporaryDirectory(prefix="ghostlab-export-test-") as directory:
            path = write_capsule_file(Path(directory) / "failure.json", capsule)
            inspection = inspect_capsule_file(path)
            self.assertEqual(inspection.schema_version, 3)
            self.assertTrue(inspection.recorded_replay.supported)
            payload = json.loads(path.read_text(encoding="utf-8"))["payload"]
            reset_scenario = scenario_from_world(payload["scenario"]).model_dump(
                mode="json")
            result = replay_recorded(
                registry=register_refund_adapters(), world_version=REFUND,
                fixture=payload["fixture"], scenario=reset_scenario,
                actor_id=payload["actor_id"], recorded_trace=payload["run"]["trace"],
                recorded_state=payload["run"]["final_state"],
                recorded_checks=payload["run"]["checks"])
            self.assertTrue(result.reproduced, result.to_dict())

    def test_retest_envelope_marks_new_experiment(self):
        registry, fixture, scenario, attempt, checks, fired = failing_run()
        capsule = export_refund_run_v3(
            fixture=asdict(fixture), timeout_after_commit=True, max_tool_calls=20,
            actor_id=fixture.customer_id, adapter="vulnerable",
            adapter_version=target_version(registry), status=attempt.status,
            trace=attempt.trace, final_state=attempt.state,
            checks=check_verdicts(checks), faults_fired=sorted(fired),
            replay_kind="retest", retest_adapter_id="fixed-agent",
            retest_adapter_version="agents/2")
        inspection = inspect_capsule(capsule.model_dump(mode="json"))
        self.assertEqual(inspection.replay_kind, "retest")
        self.assertFalse(inspection.recorded_replay.supported)

    def test_fixture_digest_enforced(self):
        envelope = _envelope_dict()
        envelope["payload"]["fixture"]["order_id"] = "tampered"
        envelope["sha256"] = capsule_digest(envelope["payload"])
        with self.assertRaises(Exception):
            CapsuleV3.model_validate(envelope)

    def test_non_pack_world_rejected(self):
        envelope = _envelope_dict()
        envelope["payload"]["world_version"] = "reservation-world/1"
        envelope["payload"]["scenario"]["world_version"] = "reservation-world/1"
        envelope["payload"]["run"]["world_version"] = "reservation-world/1"
        envelope["sha256"] = capsule_digest(envelope["payload"])
        with self.assertRaises(Exception):
            CapsuleV3.model_validate(envelope)

    def test_write_size_cap(self):
        envelope = _envelope_dict()
        with tempfile.TemporaryDirectory(prefix="ghostlab-export-test-") as directory:
            with self.assertRaisesRegex(ValueError, "too large"):
                write_capsule_file(Path(directory) / "x.json", envelope, max_bytes=10)

    def test_v2_upgrade_inspects_as_recorded(self):
        frozen = json.loads((EXAMPLES / "capsule_v2.refund_failure.json").read_text(
            encoding="utf-8"))
        upgraded = capsule_v2_to_v3(frozen).model_dump(mode="json")
        inspection = inspect_capsule(upgraded)
        self.assertEqual(inspection.schema_version, 3)
        self.assertIsNone(inspection.fixture_name)
        self.assertTrue(inspection.recorded_replay.supported)

    def test_generic_export_validates_shapes(self):
        envelope = _envelope_dict()["payload"]
        capsule = export_v3(world_version=envelope["world_version"],
                            fixture=envelope["fixture"], scenario=envelope["scenario"],
                            actor_id=envelope["actor_id"], source=envelope["source"],
                            seed=envelope["seed"],
                            recorded_model_outputs=envelope["recorded_model_outputs"],
                            run=envelope["run"], provenance=envelope["provenance"])
        self.assertEqual(capsule.payload.world_version, REFUND)
        with self.assertRaises(ValueError):
            export_v3(world_version="", fixture={}, scenario={}, actor_id="a",
                      source="s", seed=0, recorded_model_outputs=[], run={},
                      provenance=None)


def target_version(registry):
    return registry.target_manifest(REFUND, "vulnerable").version


def _envelope_dict():
    registry, fixture, scenario, attempt, checks, fired = failing_run()
    return export_refund_run_v3(
        fixture=asdict(fixture), timeout_after_commit=True, max_tool_calls=20,
        actor_id=fixture.customer_id, adapter="vulnerable",
        adapter_version=target_version(registry), status=attempt.status,
        trace=attempt.trace, final_state=attempt.state,
        checks=check_verdicts(checks),
        faults_fired=sorted(fired)).model_dump(mode="json")


if __name__ == "__main__":
    unittest.main()
