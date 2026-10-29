import json
from pathlib import Path
import tempfile
import unittest

from ghostlab.capsule import _digest, export_capsule, replay
from ghostlab.core import (BudgetExceeded, Fixture, PaymentTimeout, RefundWorld,
                           Scenario, ToolError, experiment, run, verify)


class RefundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_root = Path(__file__).resolve().parents[1] / "artifacts" / "local" / "test-temp"
        cls.temp_root.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        self.fixture = Fixture()
        self.identity = {"order_id": self.fixture.order_id,
                         "customer_id": self.fixture.customer_id}
        self.request = {**self.identity, "amount_minor": 250_000, "idempotency_key": "intent-1"}

    def test_timeout_commits_before_error(self):
        world = RefundWorld(self.fixture, Scenario(timeout_after_commit=True))
        with self.assertRaises(PaymentTimeout):
            world.call("issue_refund", self.request)
        self.assertEqual(world.snapshot()["total_refunded_minor"], 250_000)
        self.assertEqual(world.trace[0]["outcome"]["error"], "PaymentTimeout")

    def test_full_experiment_and_earliest_divergence(self):
        result = experiment()
        self.assertTrue(all(result["baseline"]["checks"].values()))
        self.assertEqual(result["faulted"]["final_state"]["total_refunded_minor"], 500_000)
        self.assertFalse(result["faulted"]["checks"]["total_refund_within_payment"])
        self.assertTrue(all(result["repaired"]["checks"].values()))
        self.assertEqual(result["repaired"]["final_state"]["total_refunded_minor"], 250_000)
        self.assertEqual(result["diff"]["first_divergence"]["event_id"], 2)

    def test_authorization_prevents_mutation(self):
        world = RefundWorld(self.fixture, Scenario())
        with self.assertRaisesRegex(ToolError, "not authorized"):
            world.call("issue_refund", {**self.request, "customer_id": "other-customer"})
        self.assertEqual(world.ledger, [])

    def test_bound_actor_cannot_spoof_customer_argument(self):
        world = RefundWorld(self.fixture, Scenario(), actor_id="unauthorized-principal")
        with self.assertRaisesRegex(ToolError, "not authorized"):
            world.call("issue_refund", self.request)
        self.assertEqual(world.ledger, [])

    def test_unknown_tools_and_malformed_amounts_rejected(self):
        for tool, request in [("shell", {}), ("issue_refund", {**self.request, "amount_minor": True}),
                              ("issue_refund", {**self.request, "extra": "ignored?"}),
                              ("issue_refund", {**self.request, "amount_minor": -1})]:
            world = RefundWorld(self.fixture, Scenario())
            with self.subTest(tool=tool, request=request), self.assertRaises(ToolError):
                world.call(tool, request)
            self.assertEqual(world.ledger, [])

    def test_idempotency_retry_and_conflicting_amount(self):
        world = RefundWorld(self.fixture, Scenario())
        first = world.call("issue_refund", self.request)
        self.assertEqual(world.call("issue_refund", self.request), first)
        self.assertEqual(len(world.ledger), 1)
        with self.assertRaisesRegex(ToolError, "different amount"):
            world.call("issue_refund", {**self.request, "amount_minor": 100})

    def test_branch_state_and_result_snapshots_do_not_alias(self):
        left = RefundWorld(self.fixture, Scenario())
        right = RefundWorld(self.fixture, Scenario())
        result = left.call("issue_refund", self.request)
        result["amount_minor"] = 1
        self.assertEqual(left.ledger[0]["amount_minor"], 250_000)
        self.assertEqual(right.ledger, [])
        snapshot = left.snapshot()
        snapshot["ledger"].clear()
        self.assertEqual(len(left.ledger), 1)

    def test_budget_stops_execution(self):
        result = run(self.fixture, Scenario(max_tool_calls=1), "vulnerable")
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["final_state"]["ledger"], [])

    def test_verifier_detects_unauthorized_record(self):
        state = {"ledger": [{**self.request, "customer_id": "attacker"}]}
        self.assertFalse(verify(self.fixture, state)["authorized_refunds"])

    def test_capsule_replay_and_tampering(self):
        scenario = Scenario(timeout_after_commit=True)
        result = run(self.fixture, scenario, "vulnerable")
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            path = Path(directory) / "failure.json"
            export_capsule(path, self.fixture, scenario, result)
            reproduced = replay(path)
            self.assertTrue(reproduced["reproduced"])
            self.assertEqual(reproduced["model_calls"], 0)
            envelope = json.loads(path.read_text())
            envelope["payload"]["run"]["final_state"]["total_refunded_minor"] = 1
            path.write_text(json.dumps(envelope))
            with self.assertRaisesRegex(ValueError, "checksum"):
                replay(path)
            envelope["sha256"] = _digest(envelope["payload"])
            path.write_text(json.dumps(envelope))
            self.assertFalse(replay(path)["reproduced"])

    def test_capsule_schema_version_rejected(self):
        result = run(self.fixture, Scenario(), "repaired")
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            path = Path(directory) / "failure.json"
            export_capsule(path, self.fixture, Scenario(), result)
            envelope = json.loads(path.read_text())
            envelope["payload"]["schema_version"] = 99
            envelope["sha256"] = _digest(envelope["payload"])
            path.write_text(json.dumps(envelope))
            with self.assertRaisesRegex(ValueError, "schema"):
                replay(path)

    def test_capsule_malformed_request_rejected(self):
        result = run(self.fixture, Scenario(), "repaired")
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            path = Path(directory) / "failure.json"
            export_capsule(path, self.fixture, Scenario(), result)
            envelope = json.loads(path.read_text())
            envelope["payload"]["run"]["trace"][0]["request"]["arguments"] = ["invalid"]
            envelope["sha256"] = _digest(envelope["payload"])
            path.write_text(json.dumps(envelope))
            with self.assertRaisesRegex(ValueError, "tool request"):
                replay(path)

    def test_capsule_malformed_fixture_and_scenario_rejected(self):
        result = run(self.fixture, Scenario(), "repaired")
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            path = Path(directory) / "failure.json"
            for field in ("fixture", "scenario"):
                with self.subTest(field=field):
                    export_capsule(path, self.fixture, Scenario(), result)
                    envelope = json.loads(path.read_text())
                    envelope["payload"][field] = None
                    envelope["sha256"] = _digest(envelope["payload"])
                    path.write_text(json.dumps(envelope))
                    with self.assertRaisesRegex(ValueError, "fixture or scenario"):
                        replay(path)

    def test_capsule_principal_mismatch_rejected(self):
        result = run(self.fixture, Scenario(), "repaired")
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            path = Path(directory) / "failure.json"
            export_capsule(path, self.fixture, Scenario(), result)
            envelope = json.loads(path.read_text())
            envelope["payload"]["actor_id"] = "another-principal"
            envelope["sha256"] = _digest(envelope["payload"])
            path.write_text(json.dumps(envelope))
            with self.assertRaisesRegex(ValueError, "principal"):
                replay(path)


if __name__ == "__main__":
    unittest.main()
