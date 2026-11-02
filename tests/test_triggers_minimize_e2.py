"""Track E2: occurrence-targeted triggers, minimization, regression export.

Every test uses track-private temporary databases (a fresh experiment store
plus a fresh environment registry per case, under the system temp dir); no
live registry file is touched. Zero model calls, no secrets.
"""

import contextlib
import gc
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from ghostlab.core import Scenario
from ghostlab.envs import STATUS_QUALIFIED
from ghostlab.envs.adapters import (REFUND_MODES, REFUND_TOOLS,
                                    RefundEnvAdapter)
from ghostlab.envs.qualification import run_qualification
from ghostlab.envs.registry import EnvRegistry
from ghostlab.experiments import ops
from ghostlab.experiments.minimize import (minimize_prefix,
                                           refund_rules_violated,
                                           replay_actions)
from ghostlab.experiments.regress import export_guard_py
from ghostlab.experiments.store import ExperimentStore

REFUND_REF = "ghostlab.envs.adapters:RefundEnvAdapter"
LATE_ARM_REF = f"{__name__}:LateArmAdapter"
CHECK_DOTTED = "ghostlab.experiments.minimize.refund_rules_violated"
FIXTURE = {"order_id": "synthetic-order-1",
           "customer_id": "synthetic-customer-1",
           "paid_minor": 250000, "currency": "INR"}
TRIGGER = {"type": "occurrence", "tool": "issue_refund", "occurrence": 2}


class LateArmAdapter(RefundEnvAdapter):
    """Refund adapter whose set_fault also arms the live world.

    The stock adapter consumes fault arming only at reset; per-trigger
    arming (arm before the counted dispatch) needs set_fault to take effect
    between reset and invoke. This test adapter adds exactly that: arming
    flips the live world's scenario, clearing restores it. Firing stays
    once-only via the world's recorded outcomes.
    """

    def set_fault(self, name, params):
        super().set_fault(name, params)
        if self._world is not None:
            self._world.scenario = Scenario(
                timeout_after_commit=True,
                max_tool_calls=self._world.scenario.max_tool_calls)

    def clear_faults(self):
        super().clear_faults()
        if self._world is not None:
            self._world.scenario = Scenario(
                timeout_after_commit=False,
                max_tool_calls=self._world.scenario.max_tool_calls)


@contextlib.contextmanager
def temp_dir():
    base = "/tmp" if os.path.isdir("/tmp") else None
    with tempfile.TemporaryDirectory(prefix="track-e2-", dir=base) as directory:
        yield directory
        gc.collect()


def valid_policy(**overrides):
    policy = {"policy_version": "policy/1", "ceiling_tool_calls": 20,
              "ceiling_total_tool_calls": 200, "ceiling_repetitions": 5,
              "ceiling_wall_clock_ms": 60000,
              "allowed_agents": ["vulnerable", "repaired"],
              "allowed_hooks": ["timeout_after_commit"]}
    policy.update(overrides)
    return policy


def valid_requested(**overrides):
    obj = {"config_version": 1, "name": "e2 trigger probe",
           "env_id": "refund-env", "agent": "vulnerable",
           "fixture": dict(FIXTURE), "faults": [],
           "rules": ["authorized_refunds", "total_refund_within_payment",
                     "no_duplicate_full_refund", "unique_idempotency_keys"],
           "limits": {"max_tool_calls": 20, "max_total_tool_calls": 200,
                      "max_wall_clock_ms": 60000},
           "repetitions": {"count": 1}}
    obj.update(overrides)
    return obj


def triggered_fault(tool="issue_refund", occurrence=2, required=True):
    return {"hook": "timeout_after_commit", "params": {},
            "required": required,
            "trigger": {"type": "occurrence", "tool": tool,
                        "occurrence": occurrence}}


def execute(directory, name, adapter_ref, agent, faults):
    """Register, qualify, resolve and run one case; return the run record."""
    registry = EnvRegistry(
        Path(directory) / f"{name}-env.sqlite",
        allowed_adapter_prefixes=("ghostlab.", __name__))
    registry.register(
        env_id="refund-env", env_version="refund-env/1",
        adapter_ref=adapter_ref, display_name="Synthetic refund",
        declared_tools=deepcopy(REFUND_TOOLS),
        declared_modes=list(REFUND_MODES))
    report = run_qualification(registry, "refund-env")
    if report["status"] != STATUS_QUALIFIED:
        raise AssertionError(f"adapter failed to qualify: {report}")
    store = ExperimentStore(Path(directory) / f"{name}-exp.sqlite")
    created = ops.create_requested_config(
        store, "proj-e2", valid_requested(agent=agent, faults=faults),
        valid_policy(), "local-operator")
    resolved = ops.resolve_config(store, registry, "proj-e2",
                                  created["config_id"], valid_policy(),
                                  "local-operator")
    run = ops.start_run(store, registry, "proj-e2",
                        resolved["resolved_id"])
    return run


def trace_actions(run):
    return [{"tool": event["request"]["tool"],
             "arguments": dict(event["request"]["arguments"])}
            for event in run["run"]["evidence"]["trace"]]


def timeout_events(run):
    return [event for event in run["run"]["evidence"]["trace"]
            if isinstance(event.get("outcome"), dict)
            and event["outcome"].get("error") == "PaymentTimeout"]


class TriggerValidationTests(unittest.TestCase):
    def check_rejected(self, faults, fragment):
        with temp_dir() as directory:
            store = ExperimentStore(Path(directory) / "exp.sqlite")
            with self.assertRaisesRegex(ValueError, fragment):
                ops.create_requested_config(store, "proj-e2",
                                            valid_requested(faults=faults),
                                            valid_policy(), "local-operator")

    def test_bad_trigger_type_rejected(self):
        for trigger in ({"type": "delay", "tool": "issue_refund",
                         "occurrence": 1},
                        {"type": "OCCURRENCE", "tool": "issue_refund",
                         "occurrence": 1},
                        {"type": 1, "tool": "issue_refund",
                         "occurrence": 1},
                        {"tool": "issue_refund", "occurrence": 1}):
            with self.subTest(trigger=trigger):
                self.check_rejected(
                    [{"hook": "timeout_after_commit", "params": {},
                      "required": True, "trigger": trigger}],
                    "trigger")

    def test_bad_trigger_tool_rejected(self):
        for tool in ("", "has space", "UPPER", "issue-refund!", 42, None):
            with self.subTest(tool=tool):
                self.check_rejected(
                    [{"hook": "timeout_after_commit", "params": {},
                      "required": True,
                      "trigger": {"type": "occurrence", "tool": tool,
                                  "occurrence": 1}}],
                    "trigger.tool")

    def test_bad_trigger_occurrence_rejected(self):
        for occurrence in (0, 65, -1, "2", 1.0, True, None):
            with self.subTest(occurrence=occurrence):
                self.check_rejected(
                    [{"hook": "timeout_after_commit", "params": {},
                      "required": True,
                      "trigger": {"type": "occurrence",
                                  "tool": "issue_refund",
                                  "occurrence": occurrence}}],
                    "occurrence")

    def test_trigger_extra_key_rejected(self):
        self.check_rejected(
            [{"hook": "timeout_after_commit", "params": {},
              "required": True,
              "trigger": {"type": "occurrence", "tool": "issue_refund",
                          "occurrence": 1, "extra": 1}}],
            "trigger")

    def test_duplicate_fault_hook_rejected(self):
        self.check_rejected(
            [{"hook": "timeout_after_commit", "params": {},
              "required": True},
             {"hook": "timeout_after_commit", "params": {},
              "required": False,
              "trigger": {"type": "occurrence", "tool": "issue_refund",
                          "occurrence": 2}}],
            "duplicate fault hook")

    def test_undiscovered_trigger_tool_rejected_at_resolve(self):
        with temp_dir() as directory:
            registry = EnvRegistry(
                Path(directory) / "env.sqlite",
                allowed_adapter_prefixes=("ghostlab.", __name__))
            registry.register(
                env_id="refund-env", env_version="refund-env/1",
                adapter_ref=LATE_ARM_REF, display_name="Synthetic refund",
                declared_tools=deepcopy(REFUND_TOOLS),
                declared_modes=list(REFUND_MODES))
            run_qualification(registry, "refund-env")
            store = ExperimentStore(Path(directory) / "exp.sqlite")
            created = ops.create_requested_config(
                store, "proj-e2",
                valid_requested(faults=[triggered_fault(
                    tool="no_such_tool")]),
                valid_policy(), "local-operator")
            with self.assertRaisesRegex(ValueError, "not implemented"):
                ops.resolve_config(store, registry, "proj-e2",
                                   created["config_id"], valid_policy(),
                                   "local-operator")

    def test_valid_triggers_resolve(self):
        with temp_dir() as directory:
            registry = EnvRegistry(
                Path(directory) / "env.sqlite",
                allowed_adapter_prefixes=("ghostlab.", __name__))
            registry.register(
                env_id="refund-env", env_version="refund-env/1",
                adapter_ref=LATE_ARM_REF, display_name="Synthetic refund",
                declared_tools=deepcopy(REFUND_TOOLS),
                declared_modes=list(REFUND_MODES))
            run_qualification(registry, "refund-env")
            store = ExperimentStore(Path(directory) / "exp.sqlite")
            for trigger in ({"type": "occurrence", "tool": "*",
                             "occurrence": 2},
                            {"type": "occurrence", "tool": "get_order",
                             "occurrence": 64}):
                with self.subTest(trigger=trigger):
                    created = ops.create_requested_config(
                        store, "proj-e2",
                        valid_requested(faults=[{
                            "hook": "timeout_after_commit", "params": {},
                            "required": True, "trigger": trigger}]),
                        valid_policy(), "local-operator")
                    resolved = ops.resolve_config(
                        store, registry, "proj-e2", created["config_id"],
                        valid_policy(), "local-operator")
                    self.assertEqual(
                        resolved["resolved"]["effective"]["faults"],
                        [{"hook": "timeout_after_commit", "params": {},
                          "required": True, "trigger": trigger}])


class OccurrenceTriggerTests(unittest.TestCase):
    def test_occurrence_2_unfired_is_fail_untested(self):
        with temp_dir() as directory:
            run = execute(directory, "occ2", LATE_ARM_REF, "vulnerable",
                          [triggered_fault("issue_refund", 2)])
            body = run["run"]
            self.assertEqual(body["status"], "completed")
            # First refund commits clean, so the agent stops: no retry, no
            # second issue_refund, the fault never fires.
            self.assertEqual(body["tool_calls"], 2)
            self.assertEqual(timeout_events(run), [])
            self.assertEqual(len(body["evidence"]["snapshot"]["ledger"]), 1)
            self.assertEqual(body["faults"],
                             [{"hook": "timeout_after_commit",
                               "required": True, "fired": False,
                               "fired_occurrence": None,
                               "trigger": dict(TRIGGER)}])
            # Fail is solely the unfired required fault: every rule passed.
            self.assertEqual(body["rules_failed"], [])
            self.assertEqual(body["verdict"], "fail")

    def test_occurrence_1_duplicates_untriggered(self):
        with temp_dir() as directory:
            triggered = execute(directory, "occ1", LATE_ARM_REF,
                                "vulnerable",
                                [triggered_fault("issue_refund", 1)])
            plain = execute(directory, "plain", LATE_ARM_REF, "vulnerable",
                            [{"hook": "timeout_after_commit", "params": {},
                              "required": True}])
            stock = execute(directory, "stock", REFUND_REF, "vulnerable",
                            [{"hook": "timeout_after_commit", "params": {},
                              "required": True}])
            self.assertEqual(triggered["run"]["evidence"]["trace"],
                             plain["run"]["evidence"]["trace"])
            self.assertEqual(triggered["run"]["evidence"]["trace"],
                             stock["run"]["evidence"]["trace"])
            self.assertEqual(triggered["run"]["tool_calls"], 3)
            self.assertEqual(triggered["run"]["verdict"], "fail")
            self.assertEqual(triggered["run"]["rules_failed"],
                             plain["run"]["rules_failed"])
            self.assertNotEqual(triggered["run"]["rules_failed"], [])
            self.assertEqual(triggered["run"]["faults"],
                             [{"hook": "timeout_after_commit",
                               "required": True, "fired": True,
                               "fired_occurrence": 1,
                               "trigger": {"type": "occurrence",
                                           "tool": "issue_refund",
                                           "occurrence": 1}}])
            # Backward compat: untriggered records keep the exact old shape.
            self.assertEqual(plain["run"]["faults"],
                             [{"hook": "timeout_after_commit",
                               "required": True, "fired": True}])

    def test_star_trigger_counts_every_dispatch(self):
        with temp_dir() as directory:
            run = execute(directory, "star", LATE_ARM_REF, "vulnerable",
                          [triggered_fault("*", 2)])
            body = run["run"]
            # Second overall dispatch is the first issue_refund: fires there.
            self.assertEqual(len(timeout_events(run)), 1)
            self.assertEqual(body["faults"][0]["fired"], True)
            self.assertEqual(body["faults"][0]["fired_occurrence"], 2)
            self.assertEqual(body["verdict"], "fail")
            self.assertNotEqual(body["rules_failed"], [])

    def test_trigger_on_get_order_never_fires(self):
        with temp_dir() as directory:
            run = execute(directory, "getorder", LATE_ARM_REF, "vulnerable",
                          [triggered_fault("get_order", 1)])
            body = run["run"]
            # Armed around get_order only; the timeout hook cannot fire
            # there, and it is disarmed before the refund commits clean.
            self.assertEqual(body["status"], "completed")
            self.assertEqual(timeout_events(run), [])
            self.assertEqual(body["faults"][0]["fired"], False)
            self.assertEqual(body["faults"][0]["fired_occurrence"], None)
            self.assertEqual(body["rules_failed"], [])
            self.assertEqual(body["verdict"], "fail")

    def test_optional_unfired_trigger_does_not_fail(self):
        with temp_dir() as directory:
            run = execute(directory, "optional", LATE_ARM_REF, "vulnerable",
                          [triggered_fault("issue_refund", 2,
                                           required=False)])
            body = run["run"]
            self.assertEqual(body["faults"][0]["fired"], False)
            self.assertEqual(body["verdict"], "pass")

    def test_stock_adapter_fires_triggered_fault(self):
        with temp_dir() as directory:
            run = execute(directory, "stocktrig", REFUND_REF, "vulnerable",
                          [triggered_fault("issue_refund", 1)])
            body = run["run"]
            # Stock adapter late-arms the live world: timeout on the first
            # refund, naive retry duplicates, fault records occurrence 1.
            self.assertEqual(body["tool_calls"], 3)
            self.assertEqual(len(timeout_events(run)), 1)
            self.assertEqual(len(body["evidence"]["snapshot"]["ledger"]), 2)
            self.assertEqual(body["faults"],
                             [{"hook": "timeout_after_commit",
                               "required": True, "fired": True,
                               "fired_occurrence": 1,
                               "trigger": {"type": "occurrence",
                                           "tool": "issue_refund",
                                           "occurrence": 1}}])
            self.assertNotEqual(body["rules_failed"], [])
            self.assertEqual(body["verdict"], "fail")

    def test_stock_adapter_occurrence_2_clean(self):
        with temp_dir() as directory:
            run = execute(directory, "stockocc2", REFUND_REF, "vulnerable",
                          [triggered_fault("issue_refund", 2)])
            body = run["run"]
            self.assertEqual(body["status"], "completed")
            self.assertEqual(body["tool_calls"], 2)
            self.assertEqual(timeout_events(run), [])
            self.assertEqual(body["faults"][0]["fired"], False)
            self.assertEqual(body["faults"][0]["fired_occurrence"], None)
            self.assertEqual(body["rules_failed"], [])
            self.assertEqual(body["verdict"], "fail")


class MinimizeTests(unittest.TestCase):
    def test_minimize_vulnerable_trace(self):
        with temp_dir() as directory:
            run = execute(directory, "vuln", REFUND_REF, "vulnerable",
                          [{"hook": "timeout_after_commit", "params": {},
                            "required": True}])
            actions = trace_actions(run)
            self.assertEqual([a["tool"] for a in actions],
                             ["get_order", "issue_refund", "issue_refund"])
            trailing = [
                {"tool": "get_order",
                 "arguments": {"order_id": FIXTURE["order_id"],
                               "customer_id": FIXTURE["customer_id"]}},
                {"tool": "get_refund_status",
                 "arguments": {"order_id": FIXTURE["order_id"],
                               "customer_id": FIXTURE["customer_id"],
                               "idempotency_key": "refund-intent-1"}}]
            result = minimize_prefix(RefundEnvAdapter, FIXTURE,
                                     actions + trailing,
                                     refund_rules_violated)
            self.assertEqual(result["required_prefix"], actions)
            self.assertEqual(result["dropped"], 2)
            self.assertTrue(result["stable"])
            self.assertEqual(result["replays"], 2)
            replayed = replay_actions(RefundEnvAdapter(), FIXTURE,
                                      result["required_prefix"])
            self.assertTrue(refund_rules_violated(replayed))

    def test_minimize_rejects_clean_trace(self):
        with temp_dir() as directory:
            run = execute(directory, "repaired", REFUND_REF, "repaired",
                          [{"hook": "timeout_after_commit", "params": {},
                            "required": True}])
            with self.assertRaisesRegex(ValueError, "nothing to minimize"):
                minimize_prefix(RefundEnvAdapter, FIXTURE,
                                trace_actions(run), refund_rules_violated)

    def test_minimize_validates_inputs(self):
        with self.assertRaises(ValueError):
            minimize_prefix(RefundEnvAdapter, FIXTURE, [],
                            refund_rules_violated)
        with self.assertRaises(ValueError):
            minimize_prefix("not-a-factory", FIXTURE,
                            [{"tool": "get_order", "arguments": {}}],
                            refund_rules_violated)
        with self.assertRaises(ValueError):
            minimize_prefix(RefundEnvAdapter, FIXTURE,
                            [{"tool": "get_order"}],
                            refund_rules_violated)


class RegressTests(unittest.TestCase):
    def run_guard(self, path):
        return subprocess.run(
            [sys.executable, "-m", "pytest", str(path), "-q"],
            capture_output=True, text=True, timeout=180)

    def test_vulnerable_guard_fails(self):
        with temp_dir() as directory:
            run = execute(directory, "vuln", REFUND_REF, "vulnerable",
                          [{"hook": "timeout_after_commit", "params": {},
                            "required": True}])
            minimized = minimize_prefix(RefundEnvAdapter, FIXTURE,
                                        trace_actions(run),
                                        refund_rules_violated)
            guard = export_guard_py(REFUND_REF, FIXTURE,
                                    minimized["required_prefix"],
                                    CHECK_DOTTED,
                                    Path(directory) / "test_guard_vuln_e2.py")
            text = Path(guard).read_text(encoding="utf-8")
            self.assertIn("sys.path.insert", text)
            self.assertIn(REFUND_REF, text)
            completed = self.run_guard(guard)
            self.assertNotEqual(
                completed.returncode, 0,
                f"vulnerable guard must FAIL; output:\n{completed.stdout}\n"
                f"{completed.stderr}")

    def test_repaired_guard_passes(self):
        with temp_dir() as directory:
            run = execute(directory, "repaired", REFUND_REF, "repaired",
                          [{"hook": "timeout_after_commit", "params": {},
                            "required": True}])
            guard = export_guard_py(REFUND_REF, FIXTURE,
                                    trace_actions(run), CHECK_DOTTED,
                                    Path(directory)
                                    / "test_guard_repaired_e2.py")
            completed = self.run_guard(guard)
            self.assertEqual(
                completed.returncode, 0,
                f"repaired guard must pass; output:\n{completed.stdout}\n"
                f"{completed.stderr}")

    def test_export_validates_inputs(self):
        with temp_dir() as directory:
            path = Path(directory) / "guard.py"
            with self.assertRaises(ValueError):
                export_guard_py("not-a-ref", FIXTURE,
                                [{"tool": "get_order", "arguments": {}}],
                                CHECK_DOTTED, path)
            with self.assertRaises(ValueError):
                export_guard_py(REFUND_REF, FIXTURE,
                                [{"tool": "get_order", "arguments": {}}],
                                "no_such_module_xyz.fn", path)
            with self.assertRaises(ValueError):
                export_guard_py(REFUND_REF, FIXTURE, [], CHECK_DOTTED,
                                path)


if __name__ == "__main__":
    unittest.main()
