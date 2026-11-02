"""Live experiment operations over operator-gated P1 worlds."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ghostlab.adapters.reservation_env import ReservationEnvironment
from ghostlab.experiments.configs import ConfigError
from ghostlab.experiments.ops import (
    RunRefused, compare_resolved, create_requested, resolve_config,
    run_repetition, validate_run_body)
from ghostlab.experiments.store import CrossProjectError, ExperimentStore
from ghostlab.extensions.environment import Action, Observation
from ghostlab.extensions.operator_registry import OperatorRegistry
from ghostlab.extensions.registry import (
    AdapterManifest, AdapterRegistry)

WORLD = "reservation-world/1"
REF = "ghostlab.adapters.reservation_env:ReservationEnvironment"


class BoomEnv(ReservationEnvironment):
    """Reset always explodes: exercises the failed-run record path."""

    def reset(self, fixture, scenario, *, actor_id):
        raise RuntimeError("boom")


class EvilEvaluateEnv(ReservationEnvironment):
    """Verdict assembly explodes after the world ran."""

    def evaluate(self, *, trace, state):
        raise RuntimeError("evil-evaluate")


class PoisonStateEnv(ReservationEnvironment):
    """State carries frozenset values JSON cannot serialize."""

    def observe(self):
        seen = super().observe()
        return Observation(trace=seen.trace,
                             state={"poison": frozenset({1})},
                             calls=seen.calls)


class SecretStateEnv(ReservationEnvironment):
    """State carries secret-shaped world text that must never persist."""

    def observe(self):
        seen = super().observe()
        state = dict(seen.state)
        state["note"] = "leaked API_KEY=abc"
        return Observation(trace=seen.trace, state=state,
                             calls=seen.calls)


class DeniedTarget:
    """Proposes one undeclared tool, then finishes."""

    def __init__(self):
        self._done = False

    def info(self):
        return None

    def start(self, task, observation):
        pass

    def propose(self, observation):
        if self._done:
            return None
        self._done = True
        return Action(tool="undeclared_tool", arguments={})

    def notify_result(self, action, outcome):
        pass

    def close(self, reason):
        pass


def fresh_operators(test_case, ref=REF):
    directory = tempfile.TemporaryDirectory(prefix="ghostlab-ops-test-")
    test_case.addCleanup(directory.cleanup)
    operator = OperatorRegistry(
        Path(directory.name) / "registry.sqlite",
        allowed_adapter_prefixes=("ghostlab.", __name__))
    operator.register(
        world_version=WORLD, environment_id="reservation-environment",
        environment_version="reservation-environment/1", adapter_ref=ref,
        display_name="Reservation demo",
        declared_tools=[{"name": "reserve", "parameters": ["holder", "seats"]}],
        declared_modes=["recorded-actions"], registered_by="operator-test")
    return operator


def fresh_store(test_case):
    directory = tempfile.TemporaryDirectory(prefix="ghostlab-ops-store-")
    test_case.addCleanup(directory.cleanup)
    return ExperimentStore(Path(directory.name) / "experiments.sqlite")


def policy(**overrides):
    body = {"policy_version": "policy/1", "ceiling_tool_calls": 50,
            "ceiling_total_tool_calls": 200, "ceiling_repetitions": 5,
            "ceiling_wall_clock_ms": 60000,
            "allowed_targets": ["reservation-target", "script", "denied-target"],
            "allowed_hooks": ["timeout_after_commit", "stale_snapshot",
                              "skip_capacity_check"]}
    body.update(overrides)
    return body


def requested(**overrides):
    body = {"config_version": 1, "name": "dev-config", "world_version": WORLD,
            "target": {"kind": "script",
                       "actions": [{"tool": "reserve",
                                    "arguments": {"holder": "amy", "seats": 1}}]},
            "fixture": {"name": "half_full"},
            "faults": [],
            "rules": ["capacity_respected"],
            "limits": {"max_tool_calls": 10, "max_total_tool_calls": 20,
                       "max_wall_clock_ms": 5000},
            "repetitions": {"count": 2}}
    body.update(overrides)
    return body


def qualify(operator):
    operator.record_qualification(
        world_version=WORLD, checks=[{"name": "ops-test", "passed": True}],
        passed=True)


def targets_with(*ids):
    targets = AdapterRegistry()
    for adapter_id in ids:
        factory = DeniedTarget if adapter_id == "denied-target" else object
        targets.register_target(
            WORLD, adapter_id, factory,
            AdapterManifest(package="test-targets",
                            version=f"{adapter_id}/1", origin="checked_in"))
    return targets


class OpsFlowTest(unittest.TestCase):
    def setup_config(self, body=None, operator=None, target_ids=("reservation-target",)):
        operator = operator or fresh_operators(self)
        store = fresh_store(self)
        saved = create_requested(
            store, project_id="proj-a", body=body or requested(),
            requested_by="dev", policy=policy())
        resolved = resolve_config(
            store, project_id="proj-a", requested_id=saved["config_id"],
            policy=policy(), operators=operator,
            targets=targets_with(*target_ids))
        return store, operator, resolved

    def test_create_rejects_bad_and_secret(self):
        store = fresh_store(self)
        bad = requested(extra=True)
        with self.assertRaises(ConfigError):
            create_requested(store, project_id="proj-a", body=bad,
                             requested_by="dev", policy=policy())
        bad = requested(name="x API_KEY=abc")
        with self.assertRaises(ConfigError):
            create_requested(store, project_id="proj-a", body=bad,
                             requested_by="dev", policy=policy())

    def test_resolve_unknown_and_cross_project(self):
        store, _, _ = self.setup_config()
        with self.assertRaises(LookupError):
            resolve_config(store, project_id="proj-a",
                           requested_id="req-0000000000000000",
                           policy=policy(), operators=fresh_operators(self),
                           targets=targets_with("reservation-target"))
        saved = store.list_requested("proj-a")[0]
        with self.assertRaises(CrossProjectError):
            resolve_config(store, project_id="proj-b",
                           requested_id=saved["config_id"],
                           policy=policy(), operators=fresh_operators(self),
                           targets=targets_with("reservation-target"))

    def test_passing_run_recorded(self):
        store, operator, resolved = self.setup_config()
        qualify(operator)
        resolved = resolve_config(
            store, project_id="proj-a",
            requested_id=resolved["requested_id"], policy=policy(),
            operators=operator, targets=targets_with("reservation-target"))
        saved = run_repetition(
            store, project_id="proj-a", resolved_id=resolved["resolved_id"],
            repetition=1, attempt=1, run_by="dev", operators=operator,
            targets=targets_with("reservation-target"))
        run = saved["run"]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["termination_reason"], "target-finished")
        self.assertEqual(run["tool_calls"], 1)
        self.assertEqual(run["failed_rules"], [])
        self.assertTrue(run["checks"]["capacity_respected"])
        self.assertEqual(run["faults_fired"], [])
        self.assertEqual(run["faults_status"], "reported")
        self.assertEqual(len(run["evidence"]["trace"]), 1)
        self.assertEqual(len(run["evidence_digest"]), 64)
        self.assertEqual(store.budget_used("proj-a", resolved["resolved_id"]), 1)

    def test_violating_run_reports_evidence(self):
        body = requested(
            target={"kind": "script", "actions": [{"tool": "reserve",
                                                  "arguments": {"holder": "amy",
                                                              "seats": 9}}]},
            faults=[{"hook": "skip_capacity_check", "params": {},
                     "required": True}])
        store, operator, resolved = self.setup_config(body)
        qualify(operator)
        resolved = resolve_config(
            store, project_id="proj-a",
            requested_id=resolved["requested_id"], policy=policy(),
            operators=operator, targets=targets_with("reservation-target"))
        saved = run_repetition(
            store, project_id="proj-a", resolved_id=resolved["resolved_id"],
            repetition=1, attempt=1, run_by="dev", operators=operator,
            targets=targets_with("reservation-target"))
        run = saved["run"]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["failed_rules"], ["capacity_respected"])
        self.assertFalse(run["checks"]["capacity_respected"])
        self.assertEqual(run["faults_fired"], ["skip_capacity_check"])

    def test_unqualified_refused_without_record(self):
        store, operator, resolved = self.setup_config()
        with self.assertRaises(RunRefused):
            run_repetition(
                store, project_id="proj-a",
                resolved_id=resolved["resolved_id"], repetition=1,
                attempt=1, run_by="dev", operators=operator,
                targets=targets_with("reservation-target"))
        self.assertEqual(store.list_runs("proj-a"), [])
        self.assertEqual(store.budget_used("proj-a", resolved["resolved_id"]), 0)

    def test_duplicate_attempt_and_repetition_bounds(self):
        store, operator, resolved = self.setup_config()
        qualify(operator)
        resolved = resolve_config(
            store, project_id="proj-a",
            requested_id=resolved["requested_id"], policy=policy(),
            operators=operator, targets=targets_with("reservation-target"))
        kwargs = {"project_id": "proj-a",
                  "resolved_id": resolved["resolved_id"], "run_by": "dev",
                  "operators": operator,
                  "targets": targets_with("reservation-target")}
        run_repetition(store, repetition=1, attempt=1, **kwargs)
        with self.assertRaises(RunRefused):
            run_repetition(store, repetition=1, attempt=1, **kwargs)
        run_repetition(store, repetition=1, attempt=2, **kwargs)
        with self.assertRaises(RunRefused):
            run_repetition(store, repetition=3, attempt=1, **kwargs)
        self.assertEqual(len(store.list_runs("proj-a")), 2)

    def test_aggregate_budget_refused(self):
        body = requested(limits={"max_tool_calls": 10,
                                 "max_total_tool_calls": 12,
                                 "max_wall_clock_ms": 5000},
            repetitions={"count": 1})
        store, operator, resolved = self.setup_config(body)
        qualify(operator)
        resolved = resolve_config(
            store, project_id="proj-a",
            requested_id=resolved["requested_id"], policy=policy(),
            operators=operator, targets=targets_with("reservation-target"))
        kwargs = {"project_id": "proj-a",
                  "resolved_id": resolved["resolved_id"], "run_by": "dev",
                  "operators": operator,
                  "targets": targets_with("reservation-target")}
        run_repetition(store, repetition=1, attempt=1, **kwargs)
        run_repetition(store, repetition=1, attempt=2, **kwargs)
        run_repetition(store, repetition=1, attempt=3, **kwargs)
        with self.assertRaises(RunRefused):
            run_repetition(store, repetition=1, attempt=4, **kwargs)

    def test_budget_exhausted_is_incomplete(self):
        body = requested(
            target={"kind": "script", "actions": [
                {"tool": "reserve",
                 "arguments": {"holder": "amy", "seats": 1}},
                {"tool": "reserve",
                 "arguments": {"holder": "bob", "seats": 1}}]},
            limits={"max_tool_calls": 1, "max_total_tool_calls": 20,
                    "max_wall_clock_ms": 5000})
        store, operator, resolved = self.setup_config(body)
        qualify(operator)
        resolved = resolve_config(
            store, project_id="proj-a",
            requested_id=resolved["requested_id"], policy=policy(),
            operators=operator, targets=targets_with("reservation-target"))
        saved = run_repetition(
            store, project_id="proj-a", resolved_id=resolved["resolved_id"],
            repetition=1, attempt=1, run_by="dev", operators=operator,
            targets=targets_with("reservation-target"))
        run = saved["run"]
        self.assertEqual(run["status"], "incomplete")
        self.assertEqual(run["termination_reason"], "budget-exhausted")
        self.assertEqual(run["limits_exceeded"], ["max_tool_calls"])

    def test_stale_target_refused_without_record(self):
        body = requested(target={"kind": "registered", "id": "reservation-target"})
        store, operator, resolved = self.setup_config(
            body, target_ids=("reservation-target",))
        qualify(operator)
        resolved = resolve_config(
            store, project_id="proj-a",
            requested_id=resolved["requested_id"], policy=policy(),
            operators=operator, targets=targets_with("reservation-target"))
        with self.assertRaises(RunRefused):
            run_repetition(
                store, project_id="proj-a",
                resolved_id=resolved["resolved_id"], repetition=1,
                attempt=1, run_by="dev", operators=operator,
                targets=AdapterRegistry())
        self.assertEqual(store.list_runs("proj-a"), [])

    def test_denied_tool_recorded(self):
        body = requested(target={"kind": "registered", "id": "denied-target"})
        store, operator, resolved = self.setup_config(
            body, target_ids=("denied-target",))
        qualify(operator)
        resolved = resolve_config(
            store, project_id="proj-a",
            requested_id=resolved["requested_id"], policy=policy(),
            operators=operator, targets=targets_with("denied-target"))
        saved = run_repetition(
            store, project_id="proj-a", resolved_id=resolved["resolved_id"],
            repetition=1, attempt=1, run_by="dev", operators=operator,
            targets=targets_with("denied-target"))
        run = saved["run"]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["termination_reason"], "denied-by-authorize")
        self.assertEqual(run["tool_calls"], 0)
        self.assertIsNotNone(run["denial"])

    def test_world_error_recorded_as_failed(self):
        operator = fresh_operators(
            self, ref=f"{__name__}:BoomEnv")
        store, _, resolved = self.setup_config(operator=operator)
        qualify(operator)
        resolved = resolve_config(
            store, project_id="proj-a",
            requested_id=resolved["requested_id"], policy=policy(),
            operators=operator, targets=targets_with("reservation-target"))
        saved = run_repetition(
            store, project_id="proj-a", resolved_id=resolved["resolved_id"],
            repetition=1, attempt=1, run_by="dev", operators=operator,
            targets=targets_with("reservation-target"))
        run = saved["run"]
        self.assertEqual(run["status"], "failed")
        self.assertIn("world-error", run["termination_reason"])
        self.assertIsNone(run["evidence"])
        self.assertIsNone(run["evidence_digest"])

    def run_on_stub(self, stub_name):
        operator = fresh_operators(self, ref=f"{__name__}:{stub_name}")
        store, _, resolved = self.setup_config(operator=operator)
        qualify(operator)
        resolved = resolve_config(
            store, project_id="proj-a",
            requested_id=resolved["requested_id"], policy=policy(),
            operators=operator, targets=targets_with("reservation-target"))
        return store, operator, resolved

    def test_evaluate_failure_records_failed_run(self):
        store, operator, resolved = self.run_on_stub("EvilEvaluateEnv")
        saved = run_repetition(
            store, project_id="proj-a",
            resolved_id=resolved["resolved_id"], repetition=1,
            attempt=1, run_by="dev", operators=operator,
            targets=targets_with("reservation-target"))
        run = saved["run"]
        self.assertEqual(run["status"], "failed")
        self.assertIn("world-error:verdict-assembly",
                         run["termination_reason"])
        self.assertEqual(run["tool_calls"], 1)
        self.assertIsNone(run["evidence"])
        self.assertEqual(store.budget_used("proj-a",
                                         resolved["resolved_id"]), 1)
        self.assertTrue(store.attempt_recorded(
            resolved["resolved_id"], 1, 1))

    def test_unserializable_evidence_records_failed_run(self):
        store, operator, resolved = self.run_on_stub("PoisonStateEnv")
        saved = run_repetition(
            store, project_id="proj-a",
            resolved_id=resolved["resolved_id"], repetition=1,
            attempt=1, run_by="dev", operators=operator,
            targets=targets_with("reservation-target"))
        run = saved["run"]
        self.assertEqual(run["status"], "failed")
        self.assertIn("world-error:verdict-assembly",
                         run["termination_reason"])
        self.assertEqual(run["tool_calls"], 1)
        self.assertIsNone(run["evidence"])

    def test_secret_evidence_never_persists(self):
        store, operator, resolved = self.run_on_stub("SecretStateEnv")
        saved = run_repetition(
            store, project_id="proj-a",
            resolved_id=resolved["resolved_id"], repetition=1,
            attempt=1, run_by="dev", operators=operator,
            targets=targets_with("reservation-target"))
        self.assertEqual(saved["run"]["status"], "failed")
        self.assertIsNone(saved["run"]["evidence"])
        connection = sqlite3.connect(store.db_path)
        try:
            rows = connection.execute(
                "SELECT run_json FROM experiment_runs").fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("API_KEY", rows[0][0])

    def test_compare_resolved(self):
        store, operator, resolved = self.setup_config()
        same = compare_resolved(
            store, project_id="proj-a", left_id=resolved["resolved_id"],
            right_id=resolved["resolved_id"])
        self.assertTrue(same["equal"])
        other = requested(rules=["capacity_respected", "used_consistent"])
        store_b = fresh_store(self)
        saved = create_requested(
            store_b, project_id="proj-a", body=other, requested_by="dev",
            policy=policy())
        resolved_b = resolve_config(
            store_b, project_id="proj-a", requested_id=saved["config_id"],
            policy=policy(), operators=operator,
            targets=targets_with("reservation-target"))
        saved_a = store.get_resolved("proj-a", resolved["resolved_id"])
        store_b.save_resolved(
            project_id="proj-a", requested_id=saved_a["requested_id"],
            body=saved_a["resolved"], digest=saved_a["digest"],
            created_at="2026-11-02T00:00:00+00:00")
        compared = compare_resolved(
            store_b, project_id="proj-a", left_id=resolved["resolved_id"],
            right_id=resolved_b["resolved_id"])
        self.assertFalse(compared["equal"])
        self.assertIn("effective.rules[1]",
                      [entry["path"] for entry in compared["diffs"]])

    def test_validate_run_body(self):
        run = {"status": "completed", "termination_reason": "target-finished",
               "tool_calls": 1, "wall_clock_ms": 5, "failed_rules": [],
               "checks": {"capacity_respected": True},
               "faults_configured": [], "faults_fired": [],
               "faults_status": "reported", "denial": None,
               "limits_exceeded": [], "evidence_digest": "0" * 64,
               "evidence": {"trace": [], "state": {}}}
        validate_run_body(run)
        bad = dict(run, status="nope")
        with self.assertRaises(ConfigError):
            validate_run_body(bad)
        bad = dict(run, checks={"capacity_respected": "yes"})
        with self.assertRaises(ConfigError):
            validate_run_body(bad)


if __name__ == "__main__":
    unittest.main()
