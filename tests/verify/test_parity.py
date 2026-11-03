"""P4 parity tests: the rule library agrees with the authoritative verifiers.

The refund rules reimplement the core invariants directly; the journey pack
rules wrap journey_verify. Either way the verdicts must match on live runs,
and completion must stay independent of safety. Standard library only.
"""

import unittest

from ghostlab.core import Fixture, experiment, verify
from ghostlab.explore.harness import ScriptedTarget
from ghostlab.adapters.refund_env import RefundEnvironment, FIXTURE_NAME
from ghostlab.extensions.environment import Action
from ghostlab.extensions.runner import run_attempt
from ghostlab.extensions.target import TargetTask
from ghostlab.verify import (FAIL, JOURNEY_RULES, PASS, REFUND_RULES, Assessment,
                             assess, journey_completion, journey_evidence,
                             refund_completion, refund_evidence)
from ghostlab.worlds.journey import JourneyFixture, JourneyScenario, scenario_json
from ghostlab.worlds.journey_run import run as journey_run
from ghostlab.worlds.journey_verify import task_outcome, verify as journey_verify

_REFUND_MAP = {"ownership_refunds": "authorized_refunds",
              "no_duplicate_full_refund": "no_duplicate_full_refund",
              "total_within_payment": "total_refund_within_payment",
              "no_duplicate_idempotency_commit": "unique_idempotency_keys"}
_PACK = ("commitments_use_approved_intent", "mandatory_participants_feasible",
         "delivered_messages_permitted")
_ADAPTERS = ("journey-naive", "journey-version-checked")


def _refund_assessment(run):
    view = refund_evidence(run["trace"], run["final_state"],
                           actor_id=run["actor_id"])
    return view, assess(view, REFUND_RULES,
                        completion=refund_completion(view.facts))


class RefundParityTests(unittest.TestCase):
    def test_rules_match_core_verify_on_all_branches(self):
        lab = experiment()
        for name in ("baseline", "faulted", "repaired"):
            run = lab[name]
            expected = verify(Fixture(), run["final_state"])
            view, assessment = _refund_assessment(run)
            self.assertEqual(assessment.unknown_rules, (), name)
            verdicts = {v.rule: v.outcome for v in assessment.verdicts}
            for rule, invariant in _REFUND_MAP.items():
                self.assertEqual(verdicts[rule], PASS if expected[invariant] else FAIL,
                                 f"{name}/{rule}")
        faulted = lab["faulted"]
        _, assessment = _refund_assessment(faulted)
        self.assertIn("total_within_payment", assessment.failed_rules)
        self.assertIn("no_duplicate_full_refund", assessment.failed_rules)


class JourneyParityTests(unittest.TestCase):
    def _scenarios(self):
        clean = JourneyScenario.from_json(scenario_json())
        faulted = JourneyScenario.from_json(scenario_json(faults=[
            ("stale_preference_memory", 1, {"preference_version": 1}),
            ("delayed_itinerary_update", None, {"delay_events": 2}),
            ("sharing_revoked_while_queued", None,
             {"recipient_id": "participant-c"}),
        ]))
        return (("clean", clean), ("faulted", faulted))

    def test_pack_rules_match_journey_verify_on_live_runs(self):
        fixture = JourneyFixture()
        for scenario_name, scenario in self._scenarios():
            for adapter_id in _ADAPTERS:
                run = journey_run(fixture, scenario, adapter_id)
                view = journey_evidence(run["trace"], run["final_state"],
                                        fixture=fixture)
                assessment = assess(view, JOURNEY_RULES,
                                    completion=journey_completion(view.facts))
                self.assertEqual(assessment.unknown_rules, (),
                                 f"{adapter_id}/{scenario_name}")
                verdicts = {v.rule: v.outcome for v in assessment.verdicts}
                for name in _PACK:
                    self.assertEqual(verdicts[name],
                                     PASS if run["checks"][name] else FAIL,
                                     f"{adapter_id}/{scenario_name}/{name}")
                self.assertEqual(
                    assessment.completion,
                    task_outcome(fixture, run["final_state"]))

    def test_authoritative_verifier_agrees_with_run_checks(self):
        fixture = JourneyFixture()
        for scenario_name, scenario in self._scenarios():
            for adapter_id in _ADAPTERS:
                run = journey_run(fixture, scenario, adapter_id)
                self.assertEqual(journey_verify(fixture, run["final_state"]),
                                 run["checks"])


class CompletionSafetySplitTests(unittest.TestCase):
    def test_incomplete_but_clean_run_holds_safety(self):
        env = RefundEnvironment()
        fixture = env.named_fixture(FIXTURE_NAME)
        env.reset(dict(fixture), {"timeout_after_commit": False,
                                  "max_tool_calls": 20},
                  actor_id=fixture["customer_id"])
        task = TargetTask(task_id="read-only",
                          params={"order_id": fixture["order_id"],
                                  "customer_id": fixture["customer_id"]})
        script = [Action(tool="get_order",
                         arguments={"order_id": fixture["order_id"],
                                    "customer_id": fixture["customer_id"]})]
        result = run_attempt(env, ScriptedTarget(script), task,
                             authorize=lambda action: None, cap=20)
        view = refund_evidence(list(result.trace), dict(result.state),
                               actor_id=fixture["customer_id"])
        assessment = assess(view, REFUND_RULES,
                            completion=refund_completion(view.facts))
        self.assertTrue(assessment.safety_held)
        self.assertFalse(assessment.completion["fully_refunded"])

    def test_assessment_summary_must_match_its_verdicts(self):
        run = experiment()["faulted"]
        view, assessment = _refund_assessment(run)
        self.assertFalse(assessment.safety_held)
        with self.assertRaises(ValueError):
            Assessment(world_version=view.world_version,
                       rules_version=assessment.rules_version,
                       verdicts=assessment.verdicts, completion=None,
                       safety_held=True,  # contradicts the failed verdicts
                       failed_rules=assessment.failed_rules,
                       unknown_rules=assessment.unknown_rules)


if __name__ == "__main__":
    unittest.main()
