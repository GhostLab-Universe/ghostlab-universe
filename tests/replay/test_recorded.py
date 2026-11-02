"""P5 replay/retest: generic recorded-action replay and fresh-agent retest."""

import unittest
from copy import deepcopy
from dataclasses import asdict

from ghostlab.adapters import register_refund_adapters
from ghostlab.core import Fixture
from ghostlab.extensions.environment import Action
from ghostlab.extensions.runner import run_attempt
from ghostlab.extensions.target import TargetTask
from ghostlab.replay.recorded import (allow_tools, check_verdicts, failure_witness,
                                      normalize_check, refuse_unless_recorded,
                                      replay_recorded, retest_fresh, witness_sha256)

REFUND = "refund-world/1"


def run_refund(adapter_id, timeout=True, cap=20):
    registry = register_refund_adapters()
    fixture = Fixture()
    scenario = {"timeout_after_commit": timeout, "max_tool_calls": cap}
    env = registry.environment_factory(REFUND)()
    target = registry.target_factory(REFUND, adapter_id)()
    env.reset(asdict(fixture), dict(scenario), actor_id=fixture.customer_id)
    task = TargetTask(task_id="p5", params={"order_id": fixture.order_id,
                                            "customer_id": fixture.customer_id})
    attempt = run_attempt(env, target, task, authorize=lambda action: None, cap=cap,
                          cooperative_stop=False)
    evaluator = registry.environment_factory(REFUND)()
    try:
        checks = evaluator.evaluate(trace=attempt.trace, state=attempt.state)
    finally:
        evaluator.close()
    return registry, fixture, scenario, attempt, checks


class RecordedTest(unittest.TestCase):
    def test_vulnerable_fails_and_replay_reproduces(self):
        registry, fixture, scenario, attempt, checks = run_refund("vulnerable")
        verdicts = check_verdicts(checks)
        self.assertIn(False, verdicts.values())
        result = replay_recorded(
            registry=registry, world_version=REFUND, fixture=asdict(fixture),
            scenario=scenario, actor_id=fixture.customer_id,
            recorded_trace=attempt.trace, recorded_state=attempt.state,
            recorded_checks=verdicts)
        self.assertTrue(result.reproduced)
        self.assertEqual(result.matches, {"trace": True, "final_state": True,
                                         "checks": True})
        self.assertIsNone(result.divergence)
        self.assertEqual(result.model_calls, 0)
        self.assertEqual(witness_sha256(result.witness), result.witness_sha256)

    def test_replay_detects_tampered_trace(self):
        registry, fixture, scenario, attempt, checks = run_refund("vulnerable")
        tampered = deepcopy(attempt.trace)
        tampered[0]["outcome"] = {"result": {"forged": True}}
        result = replay_recorded(
            registry=registry, world_version=REFUND, fixture=asdict(fixture),
            scenario=scenario, actor_id=fixture.customer_id, recorded_trace=tampered,
            recorded_state=attempt.state, recorded_checks=check_verdicts(checks))
        self.assertFalse(result.reproduced)
        self.assertFalse(result.matches["trace"])
        self.assertEqual(result.divergence["event_id"], 1)

    def test_replay_detects_wrong_checks(self):
        registry, fixture, scenario, attempt, checks = run_refund("vulnerable")
        verdicts = dict(check_verdicts(checks))
        flipped = next(name for name, hit in verdicts.items() if hit is False)
        verdicts[flipped] = True
        result = replay_recorded(
            registry=registry, world_version=REFUND, fixture=asdict(fixture),
            scenario=scenario, actor_id=fixture.customer_id,
            recorded_trace=attempt.trace, recorded_state=attempt.state,
            recorded_checks=verdicts)
        self.assertFalse(result.reproduced)
        self.assertFalse(result.matches["checks"])

    def test_unknown_world_fails_closed(self):
        registry, fixture, scenario, attempt, checks = run_refund("vulnerable")
        with self.assertRaises(LookupError):
            replay_recorded(
                registry=registry, world_version="nope/1", fixture=asdict(fixture),
                scenario=scenario, actor_id=fixture.customer_id,
                recorded_trace=attempt.trace, recorded_state=attempt.state,
                recorded_checks=check_verdicts(checks))

    def test_retest_repaired_passes_without_equivalence(self):
        registry = register_refund_adapters()
        fixture = Fixture()
        result = retest_fresh(
            registry=registry, world_version=REFUND, target_id="repaired",
            fixture=asdict(fixture),
            scenario={"timeout_after_commit": True, "max_tool_calls": 20},
            actor_id=fixture.customer_id,
            task={"task_id": "p5", "params": {"order_id": fixture.order_id,
                                             "customer_id": fixture.customer_id}},
            authorize=allow_tools("get_order", "issue_refund", "get_refund_status"),
            cap=20)
        self.assertEqual(result.failed, ())
        self.assertEqual(result.witness, {"failed": []})
        self.assertFalse(result.replay_equivalence_claimed)
        self.assertIsNone(result.model_calls)
        self.assertTrue(result.trace)
        self.assertIn("total_refunded_minor", result.final_state)
        self.assertEqual(result.target_adapter_id, "repaired")

    def test_retest_vulnerable_reports_witness(self):
        registry = register_refund_adapters()
        fixture = Fixture()
        result = retest_fresh(
            registry=registry, world_version=REFUND, target_id="vulnerable",
            fixture=asdict(fixture),
            scenario={"timeout_after_commit": True, "max_tool_calls": 20},
            actor_id=fixture.customer_id,
            task={"task_id": "p5", "params": {"order_id": fixture.order_id,
                                             "customer_id": fixture.customer_id}},
            authorize=lambda action: None, cap=20)
        self.assertTrue(result.failed)
        failed = {row["name"] for row in result.witness["failed"]}
        self.assertEqual(failed, set(result.failed))

    def test_retest_denied_names_denial(self):
        registry = register_refund_adapters()
        fixture = Fixture()
        result = retest_fresh(
            registry=registry, world_version=REFUND, target_id="vulnerable",
            fixture=asdict(fixture),
            scenario={"timeout_after_commit": False, "max_tool_calls": 20},
            actor_id=fixture.customer_id,
            task={"task_id": "p5", "params": {"order_id": fixture.order_id,
                                             "customer_id": fixture.customer_id}},
            authorize=lambda action: "policy says no", cap=20)
        self.assertTrue(result.denied)
        self.assertEqual(result.denial_summary, "policy says no")
        self.assertEqual(result.tool_calls, 0)

    def test_allow_tools(self):
        authorize = allow_tools("get_order")
        self.assertIsNone(authorize(Action(tool="get_order", arguments={})))
        self.assertEqual(authorize(Action(tool="issue_refund", arguments={})),
                         "tool_not_allowed:issue_refund")
        with self.assertRaises(TypeError):
            authorize({"tool": "get_order"})

    def test_refuse_unless_recorded(self):
        self.assertIsNone(refuse_unless_recorded(replay_kind="recorded",
                                                 truncated=False, external_names=[]))
        with self.assertRaisesRegex(ValueError, "new experiment"):
            refuse_unless_recorded(replay_kind="retest", truncated=False,
                                   external_names=[])
        with self.assertRaisesRegex(ValueError, "incomplete"):
            refuse_unless_recorded(replay_kind="recorded", truncated=True,
                                   external_names=[])
        with self.assertRaisesRegex(ValueError, "ledger-db"):
            refuse_unless_recorded(replay_kind="recorded", truncated=False,
                                   external_names=["ledger-db"])

    def test_check_helpers_reject_bad_rows(self):
        good = {"name": "x", "passed": False, "evidence_event_ids": [1],
                "observed": None, "limit": None, "unit": None}
        self.assertEqual(normalize_check(dict(good))["name"], "x")
        with self.assertRaises(ValueError):
            normalize_check({"name": "x"})
        with self.assertRaises(ValueError):
            check_verdicts([good, dict(good)])
        witness = failure_witness([good])
        self.assertEqual([row["name"] for row in witness["failed"]], ["x"])


if __name__ == "__main__":
    unittest.main()
