"""P4 exploration harness tests: bounds, determinism and scripted-only guidance.

Standard library only (unittest style).
"""

import inspect
import unittest
from pathlib import Path

from ghostlab.adapters.refund_env import FIXTURE_NAME, RefundEnvironment
from ghostlab.explore import (FIXED_ARM, GUIDED_ARM, GUIDED_MODE, RANDOM_ARM, AttemptSpec,
                              SamplingTarget, ScriptedTarget, refund_action_sampler,
                              refund_scenarios, run_campaign, run_scripted_guided)
from ghostlab.extensions.environment import Action
from ghostlab.extensions.registry import check_target
from ghostlab.extensions.target import TargetTask
from ghostlab.verify import JOURNEY_RULES, REFUND_RULES, assess, refund_completion
from ghostlab.verify import refund_evidence as _refund_evidence

IDENTITY = {"order_id": "synthetic-order-1", "customer_id": "synthetic-customer-1"}
CUSTOMER = "synthetic-customer-1"

_BANNED_IMPORTS = ("socket", "urllib", "http", "httpx", "requests", "urlopen",
                   "Baseten", "api.meta", "MODEL_API_KEY", "BASETEN")


def _assess_refund(trace, state):
    view = _refund_evidence(trace, state, actor_id=CUSTOMER)
    assessment = assess(view, REFUND_RULES,
                        completion=refund_completion(view.facts))
    return {"failed_rules": list(assessment.failed_rules),
            "unknown_rules": list(assessment.unknown_rules),
            "safety_held": assessment.safety_held}


def _task(case_id):
    return TargetTask(task_id=case_id, params=dict(IDENTITY))


def _spec(name, arm, script, scenario, cap=20):
    return AttemptSpec(name=name, arm=arm, fixture_name=FIXTURE_NAME,
                       scenario=dict(scenario), actor_id=CUSTOMER,
                       task=_task(name),
                       target_factory=lambda: ScriptedTarget(list(script)),
                       cap=cap)


def _vulnerable_script():
    return [Action(tool="get_order", arguments=dict(IDENTITY)),
            Action(tool="issue_refund",
                   arguments={**IDENTITY, "amount_minor": 250_000,
                              "idempotency_key": "k-1"}),
            Action(tool="issue_refund",
                   arguments={**IDENTITY, "amount_minor": 250_000,
                              "idempotency_key": "k-2"})]


class FixedArmTests(unittest.TestCase):
    def test_fixed_campaign_runs_bounded_and_assessed(self):
        clean, faulted = refund_scenarios()
        specs = [_spec("clean", FIXED_ARM, _vulnerable_script()[:2], clean),
                 _spec("faulted", FIXED_ARM, _vulnerable_script(), faulted)]
        report = run_campaign(specs, RefundEnvironment, assess=_assess_refund,
                              max_attempts=10)
        self.assertEqual(report.bounds["attempts_run"], 2)
        self.assertTrue(report.bounds["caps_respected"])
        self.assertIn("issue_refund", report.coverage["tools_used"])
        by_name = {a["name"]: a for a in report.attempts}
        self.assertEqual(by_name["clean"]["faults_fired"], ())
        self.assertEqual(by_name["faulted"]["faults_fired"],
                         ("timeout_after_commit",))
        self.assertTrue(by_name["faulted"]["findings"]["failed_rules"])
        self.assertIn("total_within_payment", report.coverage["rules_failed"])
        self.assertTrue(by_name["clean"]["findings"]["safety_held"])

    def test_attempt_bound_is_refused_not_truncated(self):
        clean, _ = refund_scenarios()
        specs = [_spec(f"a{i}", FIXED_ARM, _vulnerable_script()[:1], clean)
                 for i in range(3)]
        with self.assertRaises(ValueError):
            run_campaign(specs, RefundEnvironment, max_attempts=2)

    def test_action_cap_is_enforced(self):
        clean, _ = refund_scenarios()
        report = run_campaign([_spec("capped", FIXED_ARM, _vulnerable_script(),
                                     clean, cap=2)],
                              RefundEnvironment, max_attempts=10)
        attempt = report.attempts[0]
        self.assertEqual(attempt["status"], "budget_exhausted")
        self.assertEqual(attempt["tool_calls"], 2)
        self.assertTrue(report.bounds["caps_respected"])


class RandomArmTests(unittest.TestCase):
    def _random_spec(self, name, seed):
        sampler = refund_action_sampler(
            IDENTITY["order_id"], IDENTITY["customer_id"], 250_000)
        clean, _ = refund_scenarios()
        return AttemptSpec(name=name, arm=RANDOM_ARM, fixture_name=FIXTURE_NAME,
                           scenario=dict(clean), actor_id=CUSTOMER,
                           task=_task(name),
                           target_factory=lambda: SamplingTarget(sampler, seed),
                           cap=20)

    def test_same_seed_replays_identical_transcripts(self):
        first = run_campaign([self._random_spec("r1", 7)], RefundEnvironment,
                             max_attempts=10)
        second = run_campaign([self._random_spec("r1", 7)], RefundEnvironment,
                              max_attempts=10)
        attempt_a, attempt_b = first.attempts[0], second.attempts[0]
        self.assertEqual(attempt_a["trace"], attempt_b["trace"])
        self.assertEqual(attempt_a["status"], attempt_b["status"])
        self.assertEqual(attempt_a["tool_calls"], attempt_b["tool_calls"])

    def test_different_seeds_may_diverge(self):
        seen = set()
        for seed in range(10):
            report = run_campaign([self._random_spec("r", seed)],
                                  RefundEnvironment, max_attempts=10)
            trace = report.attempts[0]["trace"]
            seen.add(tuple(e["request"]["tool"] for e in trace))
        self.assertGreater(len(seen), 1)

    def test_sampler_stays_valid_and_bounded(self):
        report = run_campaign([self._random_spec("r", 3)], RefundEnvironment,
                              assess=_assess_refund, max_attempts=10)
        attempt = report.attempts[0]
        self.assertLessEqual(attempt["tool_calls"], 8)
        self.assertEqual(attempt["findings"]["unknown_rules"], [])


class GuidedArmTests(unittest.TestCase):
    def test_scripted_guided_runs_and_is_excluded_from_effectiveness(self):
        clean, _ = refund_scenarios()
        specs = [_spec("g1", GUIDED_ARM, _vulnerable_script()[:1], clean)]
        report = run_scripted_guided(specs, RefundEnvironment,
                                     assess=_assess_refund, max_attempts=10)
        self.assertEqual(report.attempts[0]["arm"], GUIDED_ARM)
        guided = report.arms[GUIDED_ARM]
        self.assertEqual(guided["mode"], GUIDED_MODE)
        self.assertTrue(guided["excluded_from_effectiveness"])

    def test_scripted_guided_refuses_other_arms(self):
        clean, _ = refund_scenarios()
        with self.assertRaises(ValueError):
            run_scripted_guided([_spec("f1", FIXED_ARM, [], clean)],
                                RefundEnvironment)

    def test_scripted_guided_takes_no_guidance_input(self):
        params = inspect.signature(run_scripted_guided).parameters
        self.assertNotIn("model", params)
        self.assertNotIn("prompt", params)
        self.assertNotIn("guidance", params)


class HarnessHygieneTests(unittest.TestCase):
    def test_targets_satisfy_the_p1_protocol(self):
        check_target(ScriptedTarget([]))
        sampler = refund_action_sampler("o", "c", 10)
        check_target(SamplingTarget(sampler, 1))

    def test_rule_counts_available_to_campaigns(self):
        self.assertEqual(len(REFUND_RULES), 7)
        self.assertEqual(len(JOURNEY_RULES), 11)

    def test_verify_and_explore_sources_have_no_banned_imports(self):
        root = Path(__file__).resolve().parents[2]
        files = sorted((root / "src" / "ghostlab" / "verify").glob("*.py"))
        files += sorted((root / "src" / "ghostlab" / "explore").glob("*.py"))
        self.assertGreaterEqual(len(files), 9)
        for path in files:
            text = path.read_text(encoding="utf-8")
            for token in _BANNED_IMPORTS:
                self.assertNotIn(token, text,
                                 f"{path.name} must stay offline: {token!r}")


if __name__ == "__main__":
    unittest.main()
