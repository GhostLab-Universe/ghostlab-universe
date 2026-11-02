"""P5 minimization: same failure witness, never just the same name."""

import unittest
from dataclasses import asdict

from ghostlab.adapters import register_refund_adapters
from ghostlab.core import Fixture
from ghostlab.extensions.runner import run_attempt
from ghostlab.extensions.target import TargetTask
from ghostlab.replay.minimize_ext import (RunEvidence, classify_evidence,
                                          minimize_faults)

REFUND = "refund-world/1"
TIMEOUT = "timeout_after_commit"


def row(name, passed, events=(1,)):
    return {"name": name, "passed": passed, "evidence_event_ids": list(events),
            "observed": None, "limit": None, "unit": None}


def evidence(status, *rows):
    return RunEvidence(status=status, checks=list(rows), tool_calls=2)


class ClassifyTest(unittest.TestCase):
    def test_same_name_different_evidence_is_not_reproduced(self):
        expected = {"failed": [row("x", False, (1, 2))]}
        trial = evidence("completed", row("x", False, (1, 3)))
        self.assertEqual(classify_evidence(trial, "x", expected), "not_reproduced")

    def test_same_witness_reproduces(self):
        expected = {"failed": [row("x", False, (1, 2))]}
        trial = evidence("completed", row("x", False, (1, 2)), row("y", True, ()))
        self.assertEqual(classify_evidence(trial, "x", expected), "reproduced")

    def test_new_failure_changes_witness(self):
        expected = {"failed": [row("x", False, (1,))]}
        trial = evidence("completed", row("x", False, (1,)), row("y", False, (2,)))
        self.assertEqual(classify_evidence(trial, "x", expected), "not_reproduced")

    def test_exhausted_nonmatch_is_inconclusive(self):
        expected = {"failed": [row("x", False, (1,))]}
        trial = evidence("budget_exhausted", row("x", True, ()))
        self.assertEqual(classify_evidence(trial, "x", expected), "inconclusive")

    def test_unknown_invariant_raises(self):
        trial = evidence("completed", row("x", False, (1,)))
        with self.assertRaisesRegex(ValueError, "Unknown invariant"):
            classify_evidence(trial, "nope", {"failed": []})


class MinimizeTest(unittest.TestCase):
    def test_already_minimal(self):
        base = evidence("completed", row("x", False, (1,)))
        run = lambda subset: base if subset == ("a",) else evidence(
            "completed", row("x", True, ()))
        report = minimize_faults(run=run, faults=["a"], invariant="x")
        self.assertEqual(report.status, "already_minimal")
        self.assertEqual(report.selected_faults, ("a",))
        self.assertTrue(report.cardinality_minimal)

    def test_reduced_to_smaller_subset(self):
        def run(subset):
            if "a" in subset:
                return evidence("completed", row("x", False, (1,)))
            return evidence("completed", row("x", True, ()))
        report = minimize_faults(run=run, faults=["a", "b"], invariant="x")
        self.assertEqual(report.status, "reduced")
        self.assertEqual(report.selected_faults, ("a",))
        self.assertEqual(report.removed_faults, ("b",))
        self.assertEqual(report.regression["faults"], ["a"])

    def test_baseline_failure_with_empty_subset(self):
        run = lambda subset: evidence("completed", row("x", False, (1,)))
        report = minimize_faults(run=run, faults=["a"], invariant="x")
        self.assertEqual(report.status, "baseline_failure")
        self.assertEqual(report.selected_faults, ())

    def test_smallest_found_on_inconclusive_smaller(self):
        def run(subset):
            if subset == ("a", "b", "c"):
                return evidence("completed", row("x", False, (1,)))
            if subset == ("a",):
                return evidence("budget_exhausted", row("x", True, ()))
            if set(subset) == {"a", "b"}:
                return evidence("completed", row("x", False, (1,)))
            return evidence("completed", row("x", True, ()))
        report = minimize_faults(run=run, faults=["a", "b", "c"], invariant="x")
        self.assertEqual(report.status, "smallest_found")
        self.assertEqual(report.selected_faults, ("a", "b"))
        self.assertFalse(report.cardinality_minimal)

    def test_budget_exhausted_reports_without_selection(self):
        base = evidence("completed", row("x", False, (1,)))
        run = lambda subset: base
        report = minimize_faults(run=run, faults=["a", "b"], invariant="x", max_runs=1)
        self.assertEqual(report.status, "run_budget_exhausted")
        self.assertIsNone(report.selected_faults)
        self.assertEqual(report.runs_used, 1)

    def test_unreproducible_baseline_raises(self):
        run = lambda subset: evidence("completed", row("x", True, ()))
        with self.assertRaisesRegex(ValueError, "cannot be reproduced"):
            minimize_faults(run=run, faults=["a"], invariant="x")

    def test_refund_timeout_is_already_minimal(self):
        registry = register_refund_adapters()
        fixture = Fixture()

        def run(subset):
            scenario = {"timeout_after_commit": TIMEOUT in subset,
                        "max_tool_calls": 20}
            env = registry.environment_factory(REFUND)()
            target = registry.target_factory(REFUND, "vulnerable")()
            env.reset(asdict(fixture), scenario, actor_id=fixture.customer_id)
            task = TargetTask(task_id="p5", params={"order_id": fixture.order_id,
                                                   "customer_id": fixture.customer_id})
            attempt = run_attempt(env, target, task, authorize=lambda a: None, cap=20,
                                  cooperative_stop=False)
            evaluator = registry.environment_factory(REFUND)()
            try:
                checks = evaluator.evaluate(trace=attempt.trace, state=attempt.state)
            finally:
                evaluator.close()
            return RunEvidence(status=attempt.status, checks=checks,
                               tool_calls=attempt.tool_calls)

        report = minimize_faults(run=run, faults=[TIMEOUT],
                                 invariant="total_refund_within_payment")
        self.assertEqual(report.status, "already_minimal")
        self.assertEqual(report.runs_used, 2)


if __name__ == "__main__":
    unittest.main()
