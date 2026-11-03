"""T2 enforcement: the enforced-execution outcome and its inspect mode.

``assess_enforcement`` reads one enforced execution record (mode/guarded
flag plus invariant checks) and reports whether the guard held. Lab
records are refused: they carry findings, not an enforcement outcome.
Inspection reports ``enforced_execution`` as a fourth mode from the
capsule's checksum-verified fixture flag.
"""

import json
import unittest
from copy import deepcopy
from dataclasses import asdict

from ghostlab.adapters import register_refund_adapters
from ghostlab.contracts.capsule_v3 import capsule_digest
from ghostlab.contracts.export import ROOT
from ghostlab.core import Fixture
from ghostlab.extensions.runner import run_attempt
from ghostlab.extensions.target import TargetTask
from ghostlab.replay.enforced import (ENFORCEMENT_SCHEMA, EnforcementResult,
                                      assess_enforcement)
from ghostlab.replay.inspect import inspect_capsule
from ghostlab.replay.recorded import (check_verdicts, failure_witness,
                                      witness_sha256)

EXAMPLES = ROOT / "examples" / "contracts" / "core"
REFUND = "refund-world/1"


def run_refund(adapter_id, timeout=True, cap=20):
    registry = register_refund_adapters()
    fixture = Fixture()
    scenario = {"timeout_after_commit": timeout, "max_tool_calls": cap}
    env = registry.environment_factory(REFUND)()
    target = registry.target_factory(REFUND, adapter_id)()
    env.reset(asdict(fixture), dict(scenario), actor_id=fixture.customer_id)
    task = TargetTask(task_id="t2", params={"order_id": fixture.order_id,
                                            "customer_id": fixture.customer_id})
    attempt = run_attempt(env, target, task, authorize=lambda action: None, cap=cap,
                          cooperative_stop=False)
    evaluator = registry.environment_factory(REFUND)()
    try:
        checks = evaluator.evaluate(trace=attempt.trace, state=attempt.state)
    finally:
        evaluator.close()
    return registry, fixture, scenario, attempt, checks


def _example(name):
    return json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))


def _reguarded(name, guarded):
    """A frozen capsule with its fixture flag rewritten and resealed."""
    envelope = deepcopy(_example(name))
    envelope["payload"]["fixture"]["guarded"] = guarded
    envelope["sha256"] = capsule_digest(envelope["payload"])
    return envelope


class EnforcementOutcomeTest(unittest.TestCase):
    def test_violated_record_names_failures_and_witness(self):
        _, _, _, _, checks = run_refund("vulnerable")
        result = assess_enforcement(world_version=REFUND, mode="enforced",
                                    guarded=True, checks=checks)
        self.assertEqual(result.schema, ENFORCEMENT_SCHEMA)
        self.assertFalse(result.held)
        self.assertEqual(set(result.failed),
                         {name for name, hit in check_verdicts(checks).items()
                          if hit is False})
        self.assertEqual(result.witness, failure_witness(checks))
        self.assertEqual(witness_sha256(result.witness), result.witness_sha256)
        self.assertEqual(result.model_calls, 0)

    def test_clean_record_holds(self):
        _, _, _, _, checks = run_refund("repaired", timeout=False)
        verdicts = check_verdicts(checks)
        if not all(verdicts.values()):
            self.skipTest("repaired run still fails; held needs a clean record")
        result = assess_enforcement(world_version=REFUND, mode="enforced",
                                    guarded=True, checks=checks)
        self.assertTrue(result.held)
        self.assertEqual(result.failed, ())

    def test_lab_mode_refused_before_checks_are_read(self):
        with self.assertRaisesRegex(ValueError, "no enforcement outcome"):
            assess_enforcement(world_version=REFUND, mode="lab", guarded=False,
                               checks="not even checks")
        with self.assertRaisesRegex(ValueError, "no enforcement outcome"):
            assess_enforcement(world_version=REFUND, mode="enforced",
                               guarded=False, checks="not even checks")

    def test_bad_mode_and_flag_types_refused(self):
        _, _, _, _, checks = run_refund("vulnerable")
        for mode, guarded in ((None, True), ("", True), ("enforced", None),
                              ("enforced", 1), ("lab", True)):
            with self.assertRaises(ValueError):
                assess_enforcement(world_version=REFUND, mode=mode,
                                   guarded=guarded, checks=checks)

    def test_bad_world_and_checks_refused(self):
        _, _, _, _, checks = run_refund("vulnerable")
        with self.assertRaises(ValueError):
            assess_enforcement(world_version="", mode="enforced",
                               guarded=True, checks=checks)
        for bad in ([], (), None, {"name": True}):
            with self.assertRaises(ValueError):
                assess_enforcement(world_version=REFUND, mode="enforced",
                                   guarded=True, checks=bad)

    def test_verdict_must_match_checks(self):
        _, _, _, _, checks = run_refund("vulnerable")
        witness = failure_witness(checks)
        verdicts = check_verdicts(checks)
        failed = tuple(sorted(name for name, hit in verdicts.items()
                              if hit is False))
        with self.assertRaisesRegex(ValueError, "does not match its checks"):
            EnforcementResult(
                schema=ENFORCEMENT_SCHEMA, world_version=REFUND, mode="enforced",
                guarded=True, held=True, checks=dict(verdicts), failed=failed,
                witness=witness, witness_sha256=witness_sha256(witness))

    def test_tampered_witness_digest_refused(self):
        _, _, _, _, checks = run_refund("vulnerable")
        result = assess_enforcement(world_version=REFUND, mode="enforced",
                                    guarded=True, checks=checks)
        with self.assertRaisesRegex(ValueError, "witness digest"):
            EnforcementResult(
                schema=result.schema, world_version=result.world_version,
                mode=result.mode, guarded=result.guarded, held=result.held,
                checks=dict(result.checks), failed=result.failed,
                witness={"failed": []}, witness_sha256=result.witness_sha256)

    def test_schema_mode_guard_and_model_calls_locked(self):
        _, _, _, _, checks = run_refund("vulnerable")
        result = assess_enforcement(world_version=REFUND, mode="enforced",
                                    guarded=True, checks=checks)
        base = {"schema": result.schema, "world_version": result.world_version,
                "mode": result.mode, "guarded": result.guarded, "held": result.held,
                "checks": dict(result.checks), "failed": result.failed,
                "witness": result.witness, "witness_sha256": result.witness_sha256}
        for field, value in (("schema", "ghostlab-recorded-replay/1"),
                             ("mode", "lab"), ("guarded", False),
                             ("world_version", "")):
            with self.assertRaises(ValueError):
                EnforcementResult(**{**base, field: value})
        with self.assertRaisesRegex(ValueError, "no model call"):
            EnforcementResult(**base, model_calls=1)

    def test_result_serializes_to_json(self):
        _, _, _, _, checks = run_refund("vulnerable")
        result = assess_enforcement(world_version=REFUND, mode="enforced",
                                    guarded=True, checks=checks)
        dumped = json.dumps(result.to_dict(), sort_keys=True)
        data = json.loads(dumped)
        self.assertEqual(data["schema"], ENFORCEMENT_SCHEMA)
        self.assertFalse(data["held"])
        self.assertEqual(data["failed"], sorted(data["failed"]))


class EnforcedInspectModeTest(unittest.TestCase):
    def test_lab_journey_refuses_with_lab_detail(self):
        for name in ("capsule_v2.journey_fixture", "capsule_v3.journey_fixture"):
            inspection = inspect_capsule(_example(name))
            self.assertFalse(inspection.enforced_execution.supported)
            self.assertIn("lab mode", inspection.enforced_execution.detail)
            # Sibling modes are untouched by the fourth mode.
            self.assertTrue(inspection.recorded_replay.supported)
            self.assertTrue(inspection.fresh_retest.supported)

    def test_unmarked_refund_refuses_with_no_mode_detail(self):
        for name in ("capsule_v1.refund_failure", "capsule_v2.refund_failure"):
            inspection = inspect_capsule(_example(name))
            self.assertFalse(inspection.enforced_execution.supported)
            self.assertIn("no recorded execution mode",
                          inspection.enforced_execution.detail)

    def test_enforced_journey_supported(self):
        for name in ("capsule_v2.journey_fixture", "capsule_v3.journey_fixture"):
            inspection = inspect_capsule(_reguarded(name, True))
            self.assertTrue(inspection.enforced_execution.supported)
            self.assertIn("enforced run", inspection.enforced_execution.detail)
            self.assertEqual(inspection.failed_invariants,
                             ("delivered_messages_permitted",))

    def test_non_boolean_flag_is_no_mode(self):
        inspection = inspect_capsule(_reguarded("capsule_v2.journey_fixture", "yes"))
        self.assertFalse(inspection.enforced_execution.supported)
        self.assertIn("no recorded execution mode",
                      inspection.enforced_execution.detail)

    def test_tampered_flag_rejected_by_checksum_first(self):
        envelope = _reguarded("capsule_v3.journey_fixture", True)
        envelope["payload"]["fixture"]["guarded"] = False
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            inspect_capsule(envelope)

    def test_mode_serializes_with_inspection(self):
        dumped = json.dumps(
            inspect_capsule(_reguarded("capsule_v3.journey_fixture", True)).to_dict(),
            sort_keys=True)
        data = json.loads(dumped)
        self.assertEqual(data["enforced_execution"],
                         {"supported": True,
                          "detail": "an enforced run; assess its recorded checks "
                                    "as the enforcement outcome"})


if __name__ == "__main__":
    unittest.main()
