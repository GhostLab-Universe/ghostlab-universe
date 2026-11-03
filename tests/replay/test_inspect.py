"""P5 inspection: checksum-first envelopes and explicit reproduction modes."""

import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from ghostlab import capsule as capsule_v1
from ghostlab.contracts.capsule_v3 import capsule_digest
from ghostlab.contracts.export import ROOT
from ghostlab.core import Fixture, Scenario, run as refund_run
from ghostlab.replay.export_v3 import export_refund_run_v3
from ghostlab.replay.inspect import inspect_capsule, inspect_capsule_file

EXAMPLES = ROOT / "examples" / "contracts" / "core"


def _v3_envelope(**overrides):
    fixture = Fixture()
    scenario = Scenario(timeout_after_commit=True, max_tool_calls=20)
    recorded = refund_run(fixture, scenario, "vulnerable", actor_id=fixture.customer_id)
    capsule = export_refund_run_v3(
        fixture=asdict(fixture), timeout_after_commit=True, max_tool_calls=20,
        actor_id=fixture.customer_id, adapter=recorded["adapter"],
        adapter_version=recorded["adapter_version"], status=recorded["status"],
        trace=recorded["trace"], final_state=recorded["final_state"],
        checks=recorded["checks"], faults_fired=["timeout_after_commit"], **overrides)
    return capsule.model_dump(mode="json")


def _example(name):
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


class InspectTest(unittest.TestCase):
    def test_frozen_v1_inspects_with_recorded_replay(self):
        inspection = inspect_capsule(_example("capsule_v1.refund_failure"))
        self.assertEqual(inspection.schema_version, 1)
        self.assertEqual(inspection.world_version, "refund-world/1")
        self.assertEqual(inspection.replay_kind, "recorded")
        self.assertTrue(inspection.recorded_replay.supported)
        self.assertTrue(inspection.fresh_retest.supported)
        self.assertTrue(inspection.failed_invariants)

    def test_frozen_v2_refund_inspects(self):
        inspection = inspect_capsule(_example("capsule_v2.refund_failure"))
        self.assertEqual(inspection.schema_version, 2)
        self.assertEqual(inspection.adapter_id, "vulnerable")
        self.assertTrue(inspection.recorded_replay.supported)

    def test_frozen_v2_journey_inspects(self):
        inspection = inspect_capsule(_example("capsule_v2.journey_fixture"))
        self.assertEqual(inspection.schema_version, 2)
        self.assertEqual(inspection.world_version, "group-journey/1")

    def test_exported_v3_inspects(self):
        inspection = inspect_capsule(_v3_envelope())
        self.assertEqual(inspection.schema_version, 3)
        self.assertEqual(inspection.fixture_name, "synthetic-inr-2500")
        self.assertEqual(inspection.faults_fired, ("timeout_after_commit",))
        self.assertTrue(inspection.recorded_replay.supported)
        self.assertIn("recorded actions", inspection.recorded_replay.detail)

    def test_truncated_refuses_recorded_replay(self):
        inspection = inspect_capsule(_v3_envelope(truncated=True))
        self.assertTrue(inspection.truncated)
        self.assertFalse(inspection.recorded_replay.supported)
        self.assertIn("incomplete", inspection.recorded_replay.detail)
        self.assertTrue(inspection.fresh_retest.supported)

    def test_external_dependencies_refuse_and_name(self):
        deps = [{"name": "ledger-db", "version": "9", "digest": None}]
        inspection = inspect_capsule(_v3_envelope(external_dependencies=deps))
        self.assertFalse(inspection.recorded_replay.supported)
        self.assertIn("ledger-db", inspection.recorded_replay.detail)

    def test_retest_kind_refuses_recorded_replay(self):
        inspection = inspect_capsule(_v3_envelope(
            replay_kind="retest", retest_adapter_id="fixed-agent",
            retest_adapter_version="agents/2"))
        self.assertEqual(inspection.replay_kind, "retest")
        self.assertFalse(inspection.recorded_replay.supported)
        self.assertIn("new experiment", inspection.recorded_replay.detail)

    def test_tampered_checksum_rejected_first(self):
        envelope = _v3_envelope()
        envelope["payload"]["seed"] = 99
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            inspect_capsule(envelope)

    def test_bad_envelope_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid capsule envelope"):
            inspect_capsule({})
        with self.assertRaisesRegex(ValueError, "Invalid capsule envelope"):
            inspect_capsule({"payload": {}})

    def test_unsupported_schema_rejected(self):
        envelope = _v3_envelope()
        envelope["payload"]["schema_version"] = 9
        envelope["sha256"] = capsule_digest(envelope["payload"])
        with self.assertRaisesRegex(ValueError, "Unsupported capsule schema"):
            inspect_capsule(envelope)

    def test_inspect_file_roundtrip(self):
        with tempfile.TemporaryDirectory(prefix="ghostlab-inspect-test-") as directory:
            path = Path(directory) / "capsule.json"
            path.write_text(json.dumps(_v3_envelope()), encoding="utf-8")
            inspection = inspect_capsule_file(path)
            self.assertEqual(inspection.schema_version, 3)
            self.assertEqual(inspect_capsule_file(str(path)).schema_version, 3)

    def test_inspect_file_rejects_missing_and_oversize(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            inspect_capsule_file(Path("no-such-capsule.json"))
        with tempfile.TemporaryDirectory(prefix="ghostlab-inspect-test-") as directory:
            path = Path(directory) / "big.json"
            path.write_text("x" * (capsule_v1.MAX_CAPSULE_BYTES + 1), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "too large"):
                inspect_capsule_file(path)

    def test_inspection_serializes_to_json(self):
        inspection = inspect_capsule(_v3_envelope())
        dumped = json.dumps(inspection.to_dict(), sort_keys=True)
        self.assertEqual(json.loads(dumped)["world_version"], "refund-world/1")


if __name__ == "__main__":
    unittest.main()
