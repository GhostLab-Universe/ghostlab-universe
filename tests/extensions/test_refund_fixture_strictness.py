"""Refund fixture strictness: partial snapshots must refuse (Blocker A)."""

import unittest
from dataclasses import asdict

from ghostlab.core import Fixture
from ghostlab.adapters.refund_env import RefundEnvironment


class RefundFixtureStrictnessTests(unittest.TestCase):
    def test_partial_and_extra_fixtures_refused(self):
        env = RefundEnvironment()
        scenario = {"timeout_after_commit": False, "max_tool_calls": 5}
        valid = asdict(Fixture())
        bad_fixtures = [
            {},
            {"order_id": "synthetic-order-1"},
            {k: v for k, v in valid.items() if k != "currency"},
            {**valid, "extra": 1},
        ]
        for bad in bad_fixtures:
            with self.assertRaises(ValueError, msg=repr(bad)):
                env.reset(bad, dict(scenario), actor_id="synthetic-customer-1")

    def test_exact_fixture_accepted_and_scenario_defaults_kept(self):
        env = RefundEnvironment()
        observed = env.reset(
            asdict(Fixture()), {}, actor_id="synthetic-customer-1")
        self.assertEqual(observed.calls, 0)
        self.assertEqual(observed.trace, [])

    def test_extension_plane_qualify_refund_passes(self):
        from ghostlab.api.ext_plane import ExtensionPlane

        plane = ExtensionPlane()
        report = plane.qualify(
            world_version="refund-world/1",
            fixture="synthetic-inr-2500",
            scenario={"timeout_after_commit": False, "max_tool_calls": 20},
            script=[{"tool": "get_order",
                     "arguments": {"order_id": "synthetic-order-1",
                                   "customer_id": "synthetic-customer-1"}}],
            task_id="onboarding-probe")
        self.assertTrue(report["passed"], report)
        for check in report["checks"]:
            self.assertTrue(check["passed"], f"{check['name']}: {check['details']}")


if __name__ == "__main__":
    unittest.main()
