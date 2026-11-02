"""P5 fidelity: measured determinism, replay, snapshots and reference diffs."""

import unittest
from copy import deepcopy
from dataclasses import asdict

from ghostlab.adapters import register_refund_adapters
from ghostlab.adapters.reservation_env import register_reservation_adapters
from ghostlab.core import Fixture
from ghostlab.extensions.environment import Action
from ghostlab.extensions.runner import run_attempt
from ghostlab.extensions.target import TargetTask
from ghostlab.replay.fidelity import assess_fidelity

REFUND = "refund-world/1"
RESERVATION = "reservation-world/1"
CLEAN_RESERVATION = {"timeout_after_commit": False, "stale_snapshot": False,
                     "skip_capacity_check": False, "max_tool_calls": 20}


def refund_script():
    """Probe actions copied from a real vulnerable run (no guessed schemas)."""
    registry = register_refund_adapters()
    fixture = Fixture()
    env = registry.environment_factory(REFUND)()
    target = registry.target_factory(REFUND, "vulnerable")()
    env.reset(asdict(fixture), {"timeout_after_commit": False, "max_tool_calls": 20},
              actor_id=fixture.customer_id)
    task = TargetTask(task_id="p5", params={"order_id": fixture.order_id,
                                            "customer_id": fixture.customer_id})
    run_attempt(env, target, task, authorize=lambda action: None, cap=20,
                cooperative_stop=False)
    return [Action(tool=item["tool"], arguments=dict(item["arguments"]))
            for item in target.info().actions]


def reservation_script():
    return [
        Action(tool="check_availability", arguments={}),
        Action(tool="reserve", arguments={"holder": "carol", "seats": 2}),
        Action(tool="get_status", arguments={"reservation_id": "R-0003"}),
        Action(tool="cancel", arguments={"reservation_id": "R-0003"}),
    ]


class FidelityTest(unittest.TestCase):
    def test_refund_is_deterministic_and_replays(self):
        registry = register_refund_adapters()
        fixture = Fixture()
        report = assess_fidelity(
            env_factory=registry.environment_factory(REFUND), fixture=asdict(fixture),
            scenario={"timeout_after_commit": True, "max_tool_calls": 20},
            actor_id=fixture.customer_id, script=refund_script(), repeats=3)
        self.assertTrue(report.passed, report.summary())
        self.assertTrue(report.determinism["identical"])
        self.assertTrue(report.replay["reproduced"])
        self.assertTrue(report.snapshot["declared"])
        self.assertTrue(report.snapshot["restored_match"])
        self.assertEqual(report.missing_capabilities, ())

    def test_reference_match_and_mismatch(self):
        registry = register_refund_adapters()
        fixture = Fixture()
        factory = registry.environment_factory(REFUND)
        script = refund_script()
        scenario = {"timeout_after_commit": False, "max_tool_calls": 20}
        first = assess_fidelity(env_factory=factory, fixture=asdict(fixture),
                                scenario=scenario, actor_id=fixture.customer_id,
                                script=script, repeats=1)
        self.assertTrue(first.passed)
        env = factory()
        try:
            env.reset(asdict(fixture), dict(scenario), actor_id=fixture.customer_id)
            for action in script:
                env.execute(action)
            observed = env.observe()
        finally:
            env.close()
        matched = assess_fidelity(
            env_factory=factory, fixture=asdict(fixture), scenario=scenario,
            actor_id=fixture.customer_id, script=script, repeats=1,
            expected_trace=list(observed.trace),
            expected_state=dict(observed.state))
        self.assertTrue(matched.reference["matched"])
        wrong_state = deepcopy(dict(observed.state))
        wrong_state["total_refunded_minor"] = 1
        diverged = assess_fidelity(
            env_factory=factory, fixture=asdict(fixture), scenario=scenario,
            actor_id=fixture.customer_id, script=script, repeats=1,
            expected_state=wrong_state)
        self.assertFalse(diverged.reference["matched"])
        self.assertFalse(diverged.passed)
        wrong_trace = deepcopy(list(observed.trace))
        wrong_trace[0] = dict(wrong_trace[0])
        wrong_trace[0]["event_id"] = 999
        diverged_trace = assess_fidelity(
            env_factory=factory, fixture=asdict(fixture), scenario=scenario,
            actor_id=fixture.customer_id, script=script, repeats=1,
            expected_trace=wrong_trace)
        self.assertEqual(diverged_trace.reference["trace_divergence"]["event_id"], 1)

    def test_declared_lists_and_qualification_echo(self):
        registry = register_refund_adapters()
        fixture = Fixture()
        report = assess_fidelity(
            env_factory=registry.environment_factory(REFUND), fixture=asdict(fixture),
            scenario={"timeout_after_commit": False, "max_tool_calls": 20},
            actor_id=fixture.customer_id, script=refund_script(), repeats=1,
            declared_tested=["reset", "replay"],
            declared_approximations=["no network latency"],
            declared_omissions=["multi-order fixtures"],
            qualification={"passed": True, "checks": 11})
        self.assertEqual(report.declared_tested, ("reset", "replay"))
        self.assertEqual(report.declared_approximations, ("no network latency",))
        self.assertEqual(report.declared_omissions, ("multi-order fixtures",))
        self.assertEqual(report.qualification, {"passed": True, "checks": 11})

    def test_reservation_reports_missing_snapshots(self):
        registry = register_reservation_adapters()
        env = registry.environment_factory(RESERVATION)()
        try:
            fixture = env.named_fixture("half_full")
        finally:
            env.close()
        report = assess_fidelity(
            env_factory=registry.environment_factory(RESERVATION), fixture=fixture,
            scenario=dict(CLEAN_RESERVATION), actor_id="qual-probe",
            script=reservation_script(), repeats=3)
        self.assertTrue(report.determinism["identical"])
        self.assertTrue(report.replay["reproduced"])
        self.assertFalse(report.snapshot["declared"])
        self.assertIn("snapshot_restore", report.missing_capabilities)
        self.assertTrue(report.passed, report.summary())

    def test_bad_inputs_rejected(self):
        registry = register_refund_adapters()
        fixture = Fixture()
        kwargs = {"env_factory": registry.environment_factory(REFUND),
                  "fixture": asdict(fixture),
                  "scenario": {"timeout_after_commit": False, "max_tool_calls": 20},
                  "actor_id": fixture.customer_id, "script": refund_script()}
        with self.assertRaisesRegex(ValueError, "repeats"):
            assess_fidelity(repeats=0, **kwargs)
        bad = dict(kwargs, script=[])
        with self.assertRaisesRegex(ValueError, "script"):
            assess_fidelity(**bad)


if __name__ == "__main__":
    unittest.main()
