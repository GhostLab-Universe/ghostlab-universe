"""P3 external target routing: plain-Python agents through the P1 TargetAdapter.

Self-play proof (zero live calls): deterministic scripted agents implement
the plain-Python interface, cross the controlled gateway, and leave
UI-neutral evidence with three distinct outcomes -- agent_retest,
recorded_replay and boundary_enforcement. Standard library only
(unittest style), so this also runs on the bare interpreter.
"""

import unittest
from collections.abc import Mapping
from dataclasses import asdict

from ghostlab.adapters.refund_env import RefundEnvironment
from ghostlab.core import Fixture
from ghostlab.extensions.environment import Action
from ghostlab.extensions.registry import RegistrationError
from ghostlab.extensions.target import (BUDGET_EXHAUSTED, DENIED, ERROR, FINISHED,
                                        TargetAdapter, TargetTask)
from ghostlab.targets import (AGENT_RETEST, BOUNDARY_ENFORCEMENT, RECORDED_REPLAY,
                              ControlledGateway, ExternalTarget,
                              ExternalTargetAdapter, check_external_target,
                              replay_recorded, run_external_attempt, summarize)

AGENT_ID = "selfplay-refund-agent"
DEFECTIVE_VERSION = "defective/1"
FIXED_VERSION = "fixed/1"
EXPLORER = {"id": "p3-selfplay-harness", "version": "p3-targets/1"}
VERIFIER_VERSION = "refund-verifier-1"

REFUND_TOOLS = {
    "get_order": frozenset({"order_id", "customer_id"}),
    "issue_refund": frozenset({"order_id", "customer_id", "amount_minor",
                               "idempotency_key"}),
    "get_refund_status": frozenset({"order_id", "customer_id", "idempotency_key"}),
}

FIRST_KEY = "ext-intent-1"
RETRY_KEY = "ext-retry-2"


class ScriptedRefundAgent:
    """Plain-Python external agent: mappings in/out, no GhostLab imports.

    Reads the order, issues one full refund, then reconciles a timeout:
    the defective agent retries with a new idempotency key (double
    refund), the fixed agent checks refund status first and only re-issues
    the original key when nothing committed.
    """

    agent_id = AGENT_ID
    model = None
    provider = None
    endpoint = None

    def __init__(self, agent_version, *, defective):
        self.agent_version = agent_version
        self._defective = defective
        self._identity = None
        self._history = []
        self._close_reason = None

    @property
    def close_reason(self):
        return self._close_reason

    def start(self, task, observation):
        if self._identity is not None:
            raise RuntimeError("Target is already started")
        if not isinstance(task, Mapping) or not isinstance(observation, Mapping):
            raise TypeError("start needs task and observation mappings")
        params = task.get("params", {})
        identity = {}
        for key in ("order_id", "customer_id"):
            value = params.get(key)
            if type(value) is not str or not value:
                raise ValueError(f"Invalid task {key}")
            identity[key] = value
        self._identity = identity
        self._history = []

    def _refund_count(self):
        return sum(1 for action, _ in self._history if action["tool"] == "issue_refund")

    def propose(self, observation):
        if self._identity is None:
            raise RuntimeError("Target was not started")
        if self._close_reason is not None:
            raise RuntimeError("Target is closed")
        if not isinstance(observation, Mapping):
            raise TypeError("propose needs an observation mapping")
        identity = dict(self._identity)
        if not self._history:
            return {"tool": "get_order", "arguments": identity}
        last_action, last_outcome = self._history[-1]
        tool, arguments = last_action["tool"], last_action["arguments"]
        if tool == "get_order":
            if last_outcome["status"] != "ok":
                return None
            return {"tool": "issue_refund",
                    "arguments": {**identity,
                                  "amount_minor": last_outcome["result"]["paid_minor"],
                                  "idempotency_key": FIRST_KEY}}
        if tool == "issue_refund" and arguments.get("idempotency_key") == FIRST_KEY:
            if self._refund_count() >= 2 or last_outcome["status"] == "ok":
                return None
            if last_outcome.get("error") != "PaymentTimeout":
                return None
            if self._defective:
                return {"tool": "issue_refund",
                        "arguments": {**identity,
                                      "amount_minor": arguments["amount_minor"],
                                      "idempotency_key": RETRY_KEY}}
            return {"tool": "get_refund_status",
                    "arguments": {**identity, "idempotency_key": FIRST_KEY}}
        if tool == "get_refund_status":
            if (last_outcome["status"] == "ok"
                    and last_outcome["result"].get("status") == "committed"):
                return None
            if self._refund_count() >= 2:
                return None
            amount = next(action["arguments"]["amount_minor"]
                          for action, _ in self._history
                          if action["tool"] == "issue_refund")
            return {"tool": "issue_refund",
                    "arguments": {**identity, "amount_minor": amount,
                                  "idempotency_key": FIRST_KEY}}
        return None

    def notify_result(self, action, outcome):
        if self._identity is None:
            raise RuntimeError("Target was not started")
        if not isinstance(action, Mapping) or not isinstance(outcome, Mapping):
            raise TypeError("notify_result needs action and outcome mappings")
        self._history.append(
            ({"tool": action["tool"], "arguments": dict(action["arguments"])},
             {"status": outcome["status"], "event_id": outcome["event_id"],
              "result": (dict(outcome["result"])
                         if outcome.get("result") is not None else None),
              "error": outcome.get("error"), "message": outcome.get("message")}))

    def close(self, reason):
        if type(reason) is not str or not reason:
            raise ValueError("Invalid close reason")
        if self._close_reason is None:
            self._close_reason = reason


class RogueToolAgent:
    """Proposes a tool outside the allowlist; must be denied pre-dispatch."""

    agent_id = AGENT_ID
    agent_version = "rogue-tool/1"
    model = None
    provider = None
    endpoint = None

    def __init__(self):
        self.close_reason = None

    def start(self, task, observation):
        pass

    def propose(self, observation):
        return {"tool": "drop_tables", "arguments": {}}

    def notify_result(self, action, outcome):
        raise AssertionError("a denied call must never report a result")

    def close(self, reason):
        self.close_reason = reason


class BadArgumentsAgent:
    """Proposes an allowlisted tool with a schema-breaking argument set."""

    agent_id = AGENT_ID
    agent_version = "rogue-args/1"
    model = None
    provider = None
    endpoint = None

    def __init__(self):
        self.close_reason = None

    def start(self, task, observation):
        pass

    def propose(self, observation):
        return {"tool": "get_order", "arguments": {"order_id": "x"}}

    def notify_result(self, action, outcome):
        raise AssertionError("a denied call must never report a result")

    def close(self, reason):
        self.close_reason = reason


class NonMappingAgent:
    """Proposes untrusted non-mapping data; the adapter must reject it."""

    agent_id = AGENT_ID
    agent_version = "rogue-shape/1"
    model = None
    provider = None
    endpoint = None

    def __init__(self):
        self.close_reason = None

    def start(self, task, observation):
        pass

    def propose(self, observation):
        return "issue_refund"

    def notify_result(self, action, outcome):
        raise AssertionError("a malformed proposal must never dispatch")

    def close(self, reason):
        self.close_reason = reason


class MutatingAgent(ScriptedRefundAgent):
    """Poisons the observation it receives; the world must be unaffected."""

    def propose(self, observation):
        observation["trace"].append({"event_id": 9999, "forged": True})
        observation["state"]["ledger"] = "forged"
        observation["calls"] = 9999
        return super().propose(observation)


def reset_env(timeout, cap=20):
    fixture_dict = asdict(Fixture())
    scenario_dict = {"timeout_after_commit": timeout, "max_tool_calls": cap}
    env = RefundEnvironment()
    env.reset(fixture_dict, scenario_dict, actor_id=fixture_dict["customer_id"])
    return env, fixture_dict, scenario_dict


def drive(agent_version, *, defective, timeout, cap=20):
    env, fixture_dict, scenario_dict = reset_env(timeout, cap)
    agent = ScriptedRefundAgent(agent_version, defective=defective)
    task = TargetTask(task_id="p3-selfplay",
                      params={"order_id": fixture_dict["order_id"],
                              "customer_id": fixture_dict["customer_id"]})
    record = run_external_attempt(
        env, agent, task, gateway=ControlledGateway(REFUND_TOOLS), cap=cap,
        explorer=EXPLORER, verifier_version=VERIFIER_VERSION, scenario=scenario_dict)
    return record, agent


class RoutingTests(unittest.TestCase):
    def test_agents_satisfy_both_interfaces(self):
        agent = ScriptedRefundAgent(FIXED_VERSION, defective=False)
        self.assertIsInstance(agent, ExternalTarget)
        self.assertIsInstance(ExternalTargetAdapter(agent), TargetAdapter)
        for rogue in (RogueToolAgent(), BadArgumentsAgent(), NonMappingAgent()):
            self.assertIsInstance(rogue, ExternalTarget)

    def test_defective_fails_fixed_passes_same_conditions(self):
        bad, _ = drive(DEFECTIVE_VERSION, defective=True, timeout=True)
        good, _ = drive(FIXED_VERSION, defective=False, timeout=True)
        self.assertEqual(bad["outcome_kind"], AGENT_RETEST)
        self.assertIn("checks_passed", bad)
        self.assertFalse(bad["checks_passed"])
        failed = [check["name"] for check in bad["checks"] if not check["passed"]]
        self.assertIn("no_duplicate_full_refund", failed)
        self.assertEqual(good["outcome_kind"], AGENT_RETEST)
        self.assertTrue(good["checks_passed"])
        self.assertEqual(bad["target"]["adapter_id"], AGENT_ID)
        self.assertEqual(bad["target"]["adapter_version"], DEFECTIVE_VERSION)
        self.assertEqual(good["target"]["adapter_version"], FIXED_VERSION)
        self.assertEqual(bad["faults_fired"], ["timeout_after_commit"])
        self.assertEqual(good["faults_fired"], ["timeout_after_commit"])
        # Same conditions: identical first two requests, then the agents diverge.
        self.assertEqual(bad["trace"][0]["request"], good["trace"][0]["request"])
        self.assertEqual(bad["trace"][1]["request"], good["trace"][1]["request"])
        self.assertNotEqual(bad["trace"][2]["request"], good["trace"][2]["request"])

    def test_defect_is_fault_triggered(self):
        record, _ = drive(DEFECTIVE_VERSION, defective=True, timeout=False)
        self.assertEqual(record["outcome_kind"], AGENT_RETEST)
        self.assertTrue(record["checks_passed"])
        self.assertEqual(record["faults_fired"], [])

    def test_identities_stay_separate(self):
        record, _ = drive(FIXED_VERSION, defective=False, timeout=True)
        self.assertEqual(record["explorer"], EXPLORER)
        self.assertEqual(record["verifier"], {"version": VERIFIER_VERSION})
        target = record["target"]
        self.assertEqual(target["adapter_id"], AGENT_ID)
        self.assertEqual(target["adapter_version"], FIXED_VERSION)
        self.assertNotEqual(record["explorer"]["id"], target["adapter_id"])
        self.assertIsNone(target["model"])
        self.assertIsNone(target["provider"])
        self.assertIsNone(target["endpoint"])
        self.assertEqual([action["tool"] for action in target["actions"]],
                         ["get_order", "issue_refund", "get_refund_status"])
        self.assertEqual([action["status"] for action in target["actions"]],
                         ["ok", "error", "ok"])
        self.assertEqual(target["close_reason"], FINISHED)

    def test_budget_exhaustion_carries_no_badge(self):
        record, _ = drive(FIXED_VERSION, defective=False, timeout=True, cap=1)
        self.assertEqual(record["outcome_kind"], AGENT_RETEST)
        self.assertEqual(record["status"], "budget_exhausted")
        self.assertEqual(record["target"]["close_reason"], BUDGET_EXHAUSTED)
        self.assertNotIn("checks_passed", record)

    def test_agent_cannot_corrupt_world_through_observation(self):
        env, fixture_dict, scenario_dict = reset_env(True)
        agent = MutatingAgent(FIXED_VERSION, defective=False)
        task = TargetTask(task_id="p3-selfplay",
                          params={"order_id": fixture_dict["order_id"],
                                  "customer_id": fixture_dict["customer_id"]})
        record = run_external_attempt(
            env, agent, task, gateway=ControlledGateway(REFUND_TOOLS), cap=20,
            explorer=EXPLORER, verifier_version=VERIFIER_VERSION,
            scenario=scenario_dict)
        self.assertTrue(record["checks_passed"])
        self.assertNotIn("forged", repr(record["trace"]))
        self.assertEqual(len(record["state"]["ledger"]), 1)

    def test_malformed_proposals_raise_before_dispatch(self):
        env, fixture_dict, scenario_dict = reset_env(True)
        agent = NonMappingAgent()
        task = TargetTask(task_id="p3-selfplay",
                          params={"order_id": fixture_dict["order_id"],
                                  "customer_id": fixture_dict["customer_id"]})
        with self.assertRaises(TypeError):
            run_external_attempt(
                env, agent, task, gateway=ControlledGateway(REFUND_TOOLS), cap=20,
                explorer=EXPLORER, verifier_version=VERIFIER_VERSION,
                scenario=scenario_dict)
        self.assertEqual(agent.close_reason, ERROR)

    def test_interface_checks_fail_closed(self):
        with self.assertRaises(RegistrationError):
            check_external_target(object())
        with self.assertRaises(RegistrationError):
            ExternalTargetAdapter(object())
        with self.assertRaises(ValueError):
            ExternalTargetAdapter(ScriptedRefundAgent("", defective=False))
        with self.assertRaises(ValueError):
            ControlledGateway({})
        env, _, _ = reset_env(True)
        with self.assertRaises(TypeError):
            run_external_attempt(env, ScriptedRefundAgent(FIXED_VERSION, defective=False),
                                 "not-a-task", gateway=ControlledGateway(REFUND_TOOLS),
                                 cap=20, explorer=EXPLORER,
                                 verifier_version=VERIFIER_VERSION)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.gateway = ControlledGateway(REFUND_TOOLS)

    def order(self, **overrides):
        arguments = {"order_id": "synthetic-order-1",
                     "customer_id": "synthetic-customer-1"}
        arguments.update(overrides)
        return Action(tool="get_order", arguments=arguments)

    def test_valid_action_is_allowed(self):
        self.assertIsNone(self.gateway.authorize(self.order()))

    def test_unknown_tool_is_denied(self):
        denial = self.gateway.authorize(Action(tool="drop_tables", arguments={}))
        self.assertEqual(denial.code, "unknown_tool")

    def test_argument_shape_is_denied(self):
        missing = self.gateway.authorize(
            Action(tool="get_order", arguments={"order_id": "x"}))
        self.assertEqual(missing.code, "invalid_arguments")
        extra = self.gateway.authorize(self.order(vip=True))
        self.assertEqual(extra.code, "invalid_arguments")

    def test_argument_values_are_denied(self):
        self.assertEqual(self.gateway.authorize(self.order(order_id="")).code,
                         "invalid_arguments")
        refund = Action(tool="issue_refund",
                        arguments={"order_id": "o", "customer_id": "c",
                                   "amount_minor": True, "idempotency_key": "k"})
        self.assertEqual(self.gateway.authorize(refund).code, "invalid_arguments")
        refund = Action(tool="issue_refund",
                        arguments={"order_id": "o", "customer_id": "c",
                                   "amount_minor": 0, "idempotency_key": "k"})
        self.assertEqual(self.gateway.authorize(refund).code, "invalid_arguments")

    def test_gateway_rejects_non_actions(self):
        with self.assertRaises(TypeError):
            self.gateway.authorize({"tool": "get_order"})


class BoundaryTests(unittest.TestCase):
    def attempt(self, agent):
        env, fixture_dict, scenario_dict = reset_env(True)
        task = TargetTask(task_id="p3-selfplay",
                          params={"order_id": fixture_dict["order_id"],
                                  "customer_id": fixture_dict["customer_id"]})
        return run_external_attempt(
            env, agent, task, gateway=ControlledGateway(REFUND_TOOLS), cap=20,
            explorer=EXPLORER, verifier_version=VERIFIER_VERSION,
            scenario=scenario_dict)

    def test_unknown_tool_denial_leaves_world_untouched(self):
        record = self.attempt(RogueToolAgent())
        self.assertEqual(record["outcome_kind"], BOUNDARY_ENFORCEMENT)
        self.assertEqual(record["denial"]["code"], "unknown_tool")
        self.assertEqual(record["trace"], [])
        self.assertEqual(record["tool_calls"], 0)
        self.assertEqual(record["state"]["ledger"], [])
        self.assertFalse(record["world_touched_by_denied_call"])
        self.assertEqual(record["target"]["close_reason"], DENIED)
        self.assertNotIn("checks_passed", record)

    def test_bad_arguments_denial_leaves_world_untouched(self):
        record = self.attempt(BadArgumentsAgent())
        self.assertEqual(record["outcome_kind"], BOUNDARY_ENFORCEMENT)
        self.assertEqual(record["denial"]["code"], "invalid_arguments")
        self.assertEqual(record["trace"], [])
        self.assertNotIn("checks_passed", record)


class ReplayTests(unittest.TestCase):
    def test_recorded_replay_matches_without_running_agent(self):
        retest, _ = drive(FIXED_VERSION, defective=False, timeout=True)
        env, _, _ = reset_env(True)
        replay = replay_recorded(
            env, retest["trace"], explorer=EXPLORER,
            verifier_version=VERIFIER_VERSION,
            source={"outcome_kind": retest["outcome_kind"],
                    "adapter_id": retest["target"]["adapter_id"],
                    "adapter_version": retest["target"]["adapter_version"]})
        self.assertEqual(replay["outcome_kind"], RECORDED_REPLAY)
        self.assertTrue(replay["matches_recording"])
        self.assertIsNone(replay["divergence"])
        self.assertEqual(replay["trace"], retest["trace"])
        self.assertEqual(replay["state"], retest["state"])
        self.assertFalse(replay["target"]["agent_reran"])
        self.assertEqual(replay["target"]["adapter_version"], FIXED_VERSION)
        self.assertNotIn("checks_passed", replay)

    def test_replay_detects_divergence(self):
        retest, _ = drive(DEFECTIVE_VERSION, defective=True, timeout=True)
        env, _, _ = reset_env(False)
        replay = replay_recorded(
            env, retest["trace"], explorer=EXPLORER,
            verifier_version=VERIFIER_VERSION,
            source={"outcome_kind": retest["outcome_kind"],
                    "adapter_id": retest["target"]["adapter_id"],
                    "adapter_version": retest["target"]["adapter_version"]})
        self.assertEqual(replay["outcome_kind"], RECORDED_REPLAY)
        self.assertFalse(replay["matches_recording"])
        self.assertEqual(replay["divergence"]["event_id"], 2)

    def test_outcomes_share_no_badge(self):
        retest, _ = drive(FIXED_VERSION, defective=False, timeout=True)
        self.assertIn("checks_passed", retest)
        env, _, _ = reset_env(True)
        replay = replay_recorded(
            env, retest["trace"], explorer=EXPLORER,
            verifier_version=VERIFIER_VERSION,
            source={"outcome_kind": retest["outcome_kind"],
                    "adapter_id": retest["target"]["adapter_id"],
                    "adapter_version": retest["target"]["adapter_version"]})
        self.assertIn("matches_recording", replay)
        self.assertNotIn("matches_recording", retest)
        self.assertNotIn("denial", replay)


class SummaryTests(unittest.TestCase):
    def test_summaries_name_versions(self):
        bad, _ = drive(DEFECTIVE_VERSION, defective=True, timeout=True)
        line = summarize(bad)
        self.assertIn(AGENT_RETEST, line)
        self.assertIn(DEFECTIVE_VERSION, line)
        self.assertIn("checks_failed", line)
        good, _ = drive(FIXED_VERSION, defective=False, timeout=True)
        self.assertIn("checks_passed", summarize(good))
        env, fixture_dict, _ = reset_env(True)
        task = TargetTask(task_id="p3-selfplay",
                          params={"order_id": fixture_dict["order_id"],
                                  "customer_id": fixture_dict["customer_id"]})
        boundary = run_external_attempt(
            env, RogueToolAgent(), task, gateway=ControlledGateway(REFUND_TOOLS),
            cap=20, explorer=EXPLORER, verifier_version=VERIFIER_VERSION)
        self.assertIn("denied=unknown_tool", summarize(boundary))
        replay_env, _, _ = reset_env(True)
        replay = replay_recorded(
            replay_env, good["trace"], explorer=EXPLORER,
            verifier_version=VERIFIER_VERSION,
            source={"outcome_kind": good["outcome_kind"],
                    "adapter_id": good["target"]["adapter_id"],
                    "adapter_version": good["target"]["adapter_version"]})
        self.assertIn("match", summarize(replay))
        with self.assertRaises(ValueError):
            summarize({})


if __name__ == "__main__":
    unittest.main()
