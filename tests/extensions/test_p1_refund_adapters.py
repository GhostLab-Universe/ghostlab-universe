"""P1 refund migration: checked-in adapters behind the frozen interfaces.

Standard library only (unittest style). Every test here compares the new
adapter path against the untouched ``ghostlab.core`` behavior: the same call
sequence through the generic runner must produce identical traces, states,
checks and statuses.
"""

import unittest
from dataclasses import asdict

from ghostlab.adapters import (default_registry, register_refund_adapters)
from ghostlab.adapters.refund_env import (ENVIRONMENT_VERSION, FIXTURE_NAME,
                                          REFUND_INVARIANTS, REFUND_TOOL_SCHEMAS,
                                          TIMEOUT_FAULT, RefundEnvironment,
                                          ledger_change_events)
from ghostlab.adapters.refund_target import ADAPTER_IDS, RefundTarget
from ghostlab.core import (ADAPTER_VERSION, Fixture, PaymentTimeout, Scenario, ToolError,
                           run as core_run, verify)
from ghostlab.extensions.environment import (Action, EnvironmentAdapter, Observation,
                                             Outcome, UnsupportedOperation)
from ghostlab.extensions.registry import (AdapterRegistry, UnknownTargetError,
                                          UnknownWorldError)
from ghostlab.extensions.runner import replay_trace, run_attempt
from ghostlab.extensions.target import TargetAdapter, TargetTask

CAPS = (1, 2, 3, 5, 20)


def drive(adapter_id, timeout, cap):
    """One adapter run through the new interfaces; returns trace, state, status."""
    fixture = Fixture()
    env = RefundEnvironment()
    env.reset(asdict(fixture),
              {"timeout_after_commit": timeout, "max_tool_calls": cap},
              actor_id=fixture.customer_id)
    target = RefundTarget(adapter_id)
    task = TargetTask(task_id="parity",
                      params={"order_id": fixture.order_id,
                              "customer_id": fixture.customer_id})
    result = run_attempt(env, target, task, authorize=lambda action: None, cap=cap,
                         cooperative_stop=False)
    return result, target


class DescribeTests(unittest.TestCase):
    def test_description(self):
        description = RefundEnvironment().describe()
        self.assertEqual(description.world_version, "refund-world/1")
        self.assertEqual(description.environment_version, ENVIRONMENT_VERSION)
        self.assertEqual([tool.name for tool in description.tools],
                         ["get_order", "issue_refund", "get_refund_status"])
        self.assertEqual([tool.effect for tool in description.tools],
                         ["read", "mutate", "read"])
        self.assertEqual([fault.fault_id for fault in description.faults],
                         [TIMEOUT_FAULT])
        self.assertTrue(description.faults[0].trigger_points)
        self.assertEqual(tuple(description.invariants), REFUND_INVARIANTS)
        self.assertEqual(tuple(description.fixtures), (FIXTURE_NAME,))
        self.assertEqual(description.capabilities["snapshot_restore"], True)

    def test_tool_schemas_match_core_dispatch(self):
        schemas = REFUND_TOOL_SCHEMAS
        self.assertEqual(sorted(schemas), ["get_order", "get_refund_status", "issue_refund"])
        self.assertEqual(schemas["get_order"]["required"], ["customer_id", "order_id"])
        self.assertEqual(schemas["issue_refund"]["required"],
                         ["amount_minor", "customer_id", "idempotency_key", "order_id"])

    def test_named_fixture(self):
        env = RefundEnvironment()
        self.assertEqual(env.named_fixture(FIXTURE_NAME), asdict(Fixture()))
        with self.assertRaises(KeyError):
            env.named_fixture("nope")

    def test_adapters_satisfy_the_protocols(self):
        self.assertIsInstance(RefundEnvironment(), EnvironmentAdapter)
        for adapter_id in ADAPTER_IDS:
            self.assertIsInstance(RefundTarget(adapter_id), TargetAdapter)


class ParityTests(unittest.TestCase):
    def test_runner_matches_core_run(self):
        compared = 0
        for adapter_id in ADAPTER_IDS:
            for timeout in (False, True):
                for cap in CAPS:
                    scenario = Scenario(timeout_after_commit=timeout, max_tool_calls=cap)
                    expected = core_run(Fixture(), scenario, adapter_id)
                    result, target = drive(adapter_id, timeout, cap)
                    self.assertEqual(result.trace, expected["trace"],
                                     (adapter_id, timeout, cap))
                    self.assertEqual(result.state, expected["final_state"],
                                     (adapter_id, timeout, cap))
                    self.assertEqual(result.status, expected["status"],
                                     (adapter_id, timeout, cap))
                    self.assertEqual(result.tool_calls, len(expected["trace"]),
                                     (adapter_id, timeout, cap))
                    self.assertEqual(target.info().adapter_version, ADAPTER_VERSION)
                    compared += 1
        self.assertEqual(compared, 2 * 2 * len(CAPS))

    def test_known_amounts(self):
        self.assertEqual(drive("vulnerable", True, 20)[0].state["total_refunded_minor"],
                         500_000)
        self.assertEqual(drive("vulnerable", False, 20)[0].state["total_refunded_minor"],
                         250_000)
        self.assertEqual(drive("repaired", True, 20)[0].state["total_refunded_minor"],
                         250_000)


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.env = RefundEnvironment()
        fixture = Fixture()
        self.identity = {"order_id": fixture.order_id, "customer_id": fixture.customer_id}
        self.env.reset(asdict(fixture), {"timeout_after_commit": False, "max_tool_calls": 5},
                       actor_id=fixture.customer_id)

    def test_non_actions_raise_before_touching_state(self):
        with self.assertRaises(TypeError):
            self.env.execute({"tool": "get_order"})
        self.assertEqual(self.env.observe().calls, 0)

    def test_unknown_tools_are_recorded_world_errors(self):
        outcome = self.env.execute(Action(tool="nope", arguments={}))
        self.assertEqual(outcome.status, "error")
        self.assertEqual(outcome.error, "ToolError")
        self.assertEqual(self.env.observe().calls, 1)

    def test_outcomes_carry_event_ids(self):
        first = self.env.execute(Action(tool="get_order", arguments=dict(self.identity)))
        self.assertEqual((first.status, first.event_id), ("ok", 1))
        self.assertEqual(first.result["paid_minor"], 250_000)

    def test_observations_are_private_copies(self):
        observed = self.env.observe()
        observed.trace.append({})
        observed.state["ledger"].append({})
        fresh = self.env.observe()
        self.assertEqual((fresh.trace, fresh.calls), ([], 0))
        self.assertEqual(fresh.state["ledger"], [])

    def test_close_ends_the_run(self):
        self.env.close()
        with self.assertRaises(RuntimeError):
            self.env.execute(Action(tool="get_order", arguments=dict(self.identity)))
        with self.assertRaises(RuntimeError):
            self.env.observe()
        self.env.close()

    def test_invalid_reset_is_refused(self):
        env = RefundEnvironment()
        with self.assertRaises(ValueError):
            env.reset({}, {"timeout_after_commit": False, "max_tool_calls": 5,
                           "extra": 1}, actor_id="x")
        with self.assertRaises(ValueError):
            env.reset(asdict(Fixture()),
                      {"timeout_after_commit": False, "max_tool_calls": 5}, actor_id="")
        with self.assertRaises(ValueError):
            env.reset("nope", {}, actor_id="x")


class EvaluateTests(unittest.TestCase):
    def test_checks_match_core_verify(self):
        for adapter_id in ADAPTER_IDS:
            for timeout in (False, True):
                result, _ = drive(adapter_id, timeout, 20)
                checks = {check.name: check.passed
                          for check in RefundEnvironment().evaluate(
                              trace=result.trace, state=result.state)}
                self.assertEqual(checks, verify(Fixture(), result.state),
                                 (adapter_id, timeout))

    def test_evidence_mapping_matches_derive_findings(self):
        result, _ = drive("vulnerable", True, 20)
        by_name = {check.name: check for check in RefundEnvironment().evaluate(
            trace=result.trace, state=result.state)}
        self.assertEqual(ledger_change_events(result.trace), [2, 3])
        failed = [name for name, check in by_name.items() if not check.passed]
        self.assertEqual(sorted(failed), ["no_duplicate_full_refund",
                                          "total_refund_within_payment"])
        for name in failed:
            self.assertEqual(by_name[name].evidence_event_ids, (2, 3), name)
        totals = by_name["total_refund_within_payment"]
        self.assertEqual((totals.observed, totals.limit, totals.unit),
                         (500_000, 250_000, "minor_units"))
        self.assertEqual(by_name["authorized_refunds"].evidence_event_ids, ())

    def test_evaluate_is_deterministic(self):
        result, _ = drive("repaired", True, 20)
        first = RefundEnvironment().evaluate(trace=result.trace, state=result.state)
        second = RefundEnvironment().evaluate(trace=result.trace, state=result.state)
        self.assertEqual(first, second)

    def test_invalid_evidence_is_refused(self):
        env = RefundEnvironment()
        with self.assertRaises(ValueError):
            env.evaluate(trace=[], state={})
        with self.assertRaises(ValueError):
            env.evaluate(trace={}, state={})


class FaultsFiredTests(unittest.TestCase):
    def test_timeout_reports_only_when_it_fired(self):
        env = RefundEnvironment()
        faulted, _ = drive("vulnerable", True, 20)
        clean, _ = drive("vulnerable", False, 20)
        scenario = {"timeout_after_commit": True, "max_tool_calls": 20}
        self.assertEqual(env.faults_fired(scenario, faulted.trace), {TIMEOUT_FAULT})
        self.assertEqual(env.faults_fired(scenario, clean.trace), frozenset())
        with self.assertRaises(ValueError):
            env.faults_fired({}, "nope")


class SnapshotTests(unittest.TestCase):
    def test_restore_continues_identically(self):
        fixture = Fixture()
        first, second = RefundEnvironment(), RefundEnvironment()
        scenario = {"timeout_after_commit": True, "max_tool_calls": 20}
        first.reset(asdict(fixture), scenario, actor_id=fixture.customer_id)
        identity = {"order_id": fixture.order_id, "customer_id": fixture.customer_id}
        first.execute(Action(tool="get_order", arguments=dict(identity)))
        snap = first.snapshot()
        continuation = [
            Action(tool="issue_refund",
                   arguments={**identity, "amount_minor": 250_000,
                              "idempotency_key": "refund-intent-1"}),
            Action(tool="issue_refund",
                   arguments={**identity, "amount_minor": 250_000,
                              "idempotency_key": "refund-retry-2"}),
        ]
        for action in continuation:
            first.execute(action)
        second.reset(asdict(fixture), scenario, actor_id=fixture.customer_id)
        second.restore(snap)
        for action in continuation:
            second.execute(action)
        self.assertEqual(second.observe().trace, first.observe().trace)
        self.assertEqual(second.observe().state, first.observe().state)

    def test_snapshot_preserves_fault_state(self):
        fixture = Fixture()
        env = RefundEnvironment()
        scenario = {"timeout_after_commit": True, "max_tool_calls": 20}
        env.reset(asdict(fixture), scenario, actor_id=fixture.customer_id)
        snap = env.snapshot()
        identity = {"order_id": fixture.order_id, "customer_id": fixture.customer_id}
        refund = Action(tool="issue_refund",
                        arguments={**identity, "amount_minor": 250_000,
                                   "idempotency_key": "refund-intent-1"})
        self.assertEqual(env.execute(refund).error, "PaymentTimeout")
        env.restore(snap)
        self.assertEqual(env.execute(refund).error, "PaymentTimeout")
        self.assertEqual(env.observe().state["total_refunded_minor"], 250_000)

    def test_corrupt_snapshots_are_refused(self):
        env = RefundEnvironment()
        with self.assertRaises(RuntimeError):
            env.restore({})
        fixture = Fixture()
        env.reset(asdict(fixture), {"timeout_after_commit": False, "max_tool_calls": 5},
                  actor_id=fixture.customer_id)
        with self.assertRaises(ValueError):
            env.restore({"trace": [], "calls": 1, "fault_fired": False})
        with self.assertRaises(ValueError):
            env.restore({"ledger": [], "trace": [], "calls": 1, "fault_fired": False})
        with self.assertRaises(RuntimeError):
            RefundEnvironment().snapshot()


class TargetTests(unittest.TestCase):
    def test_unknown_adapters_raise(self):
        with self.assertRaises(ValueError):
            RefundTarget("nope")

    def test_info_records_config_and_visible_actions(self):
        _, target = drive("repaired", True, 20)
        info = target.info()
        self.assertEqual((info.adapter_id, info.adapter_version), ("repaired", ADAPTER_VERSION))
        self.assertEqual((info.model, info.provider, info.endpoint), (None, None, None))
        self.assertEqual([action["tool"] for action in info.actions],
                         ["get_order", "issue_refund", "get_refund_status"])
        self.assertEqual([action["status"] for action in info.actions],
                         ["ok", "error", "ok"])

    def test_single_use_lifecycle(self):
        fixture = Fixture()
        target = RefundTarget("vulnerable")
        task = TargetTask(task_id="t", params={"order_id": fixture.order_id,
                                              "customer_id": fixture.customer_id})
        observation = Observation(trace=[], state={}, calls=0)
        with self.assertRaises(RuntimeError):
            target.propose(observation)
        target.start(task, observation)
        with self.assertRaises(RuntimeError):
            target.start(task, observation)
        with self.assertRaises(ValueError):
            RefundTarget("vulnerable").start(
                TargetTask(task_id="t", params={"order_id": "x"}), observation)

    def test_unexpected_world_errors_propagate(self):
        fixture = Fixture()
        env = RefundEnvironment()
        env.reset(asdict(fixture), {"timeout_after_commit": False, "max_tool_calls": 5},
                  actor_id="someone-else")
        target = RefundTarget("vulnerable")
        task = TargetTask(task_id="t", params={"order_id": fixture.order_id,
                                              "customer_id": fixture.customer_id})
        with self.assertRaises(ToolError):
            run_attempt(env, target, task, authorize=lambda action: None, cap=5,
                        cooperative_stop=False)

    def test_payment_timeout_type_is_preserved(self):
        with self.assertRaises(PaymentTimeout):
            raise PaymentTimeout("probe")
        outcome = Outcome(status="error", event_id=1, error="PaymentTimeout",
                          message="Payment committed; response timed out")
        self.assertEqual(outcome.error, "PaymentTimeout")


class RegistryTests(unittest.TestCase):
    def test_default_registry_holds_the_checked_in_refund_package(self):
        register = default_registry()
        self.assertIs(register, default_registry())
        self.assertEqual(register.worlds(), ("refund-world/1",))
        self.assertEqual(register.targets("refund-world/1"), ("repaired", "vulnerable"))
        self.assertEqual(register.environment_manifest("refund-world/1").origin,
                         "checked_in")
        self.assertIsInstance(register.environment_factory("refund-world/1")(),
                              EnvironmentAdapter)
        self.assertIsInstance(
            register.target_factory("refund-world/1", "vulnerable")(), TargetAdapter)
        with self.assertRaises(UnknownTargetError):
            register.target_factory("refund-world/1", "nope")
        with self.assertRaises(UnknownWorldError):
            register.environment_factory("nope/1")

    def test_explicit_registration(self):
        register = register_refund_adapters(AdapterRegistry())
        self.assertEqual(register.worlds(), ("refund-world/1",))


class ReplayHelperTests(unittest.TestCase):
    def test_replay_trace_reproduces_recorded_runs(self):
        for adapter_id in ADAPTER_IDS:
            for timeout in (False, True):
                result, _ = drive(adapter_id, timeout, 20)
                fixture = Fixture()
                fresh = RefundEnvironment()
                fresh.reset(asdict(fixture),
                            {"timeout_after_commit": timeout, "max_tool_calls": 20},
                            actor_id=fixture.customer_id)
                replayed = replay_trace(fresh, result.trace)
                self.assertEqual(replayed.trace, result.trace, (adapter_id, timeout))
                self.assertEqual(replayed.state, result.state, (adapter_id, timeout))


if __name__ == "__main__":
    unittest.main()