"""Track C: experiment configs, persistence, API ops, controller plumbing.

Every test uses track-private temporary databases (a fresh experiment store
plus a fresh environment registry per test, under /tmp when it exists);
no live registry file is touched.
"""

import contextlib
import gc
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path

from ghostlab.core import (ADAPTER_VERSION, WORLD_VERSION, Fixture, Scenario,
                           reality_diff)
from ghostlab.core import run as core_run
from ghostlab.envs.adapters import (REFUND_MODES, REFUND_TOOLS,
                                    RefundEnvAdapter)
from ghostlab.envs.manifest import import_manifest
from ghostlab.envs.qualification import run_qualification
from ghostlab.envs.registry import EnvRegistry
from ghostlab.experiments import ops
from ghostlab.experiments.configs import (RequestedConfig, resolved_from_dict,
                                          validate_policy, validate_requested)
from ghostlab.experiments.controller import (evidence_complete, evaluate_verdict,
                                             fault_fired)
from ghostlab.experiments.controller import BudgetExhaustedError
from ghostlab.experiments.diffs import diff_resolved
from ghostlab.experiments.store import CrossProjectError, ExperimentStore

REFUND_REF = "ghostlab.envs.adapters:RefundEnvAdapter"
CANARY = "TRACKC-CANARY-7f3a9e1d-do-not-persist"


@contextlib.contextmanager
def temp_dir():
    base = "/tmp" if os.path.isdir("/tmp") else None
    with tempfile.TemporaryDirectory(prefix="track-c-", dir=base) as directory:
        yield directory
        # The shared env registry (owned by another track) leaves sqlite
        # handles to cyclic collection; collect before rmtree so Windows
        # releases the files. Our own store closes explicitly.
        gc.collect()


class CountingAdapter(RefundEnvAdapter):
    inits = describes = resets = invokes = faults = 0

    def __init__(self):
        type(self).inits += 1
        super().__init__()

    def describe(self):
        type(self).describes += 1
        return super().describe()

    def reset(self, fixture):
        type(self).resets += 1
        return super().reset(fixture)

    def invoke(self, tool, arguments):
        type(self).invokes += 1
        return super().invoke(tool, arguments)

    def set_fault(self, name, params):
        type(self).faults += 1
        return super().set_fault(name, params)

    @classmethod
    def reset_counters(cls):
        cls.inits = cls.describes = cls.resets = cls.invokes = cls.faults = 0


class SlimAdapter(RefundEnvAdapter):
    """Implements only get_order, to test missing-required-tool rejection."""

    def describe(self):
        described = super().describe()
        described["tools"] = [
            {"name": "get_order", "parameters": ["order_id", "customer_id"]}]
        return described


def make_registry(directory, *extra_prefixes):
    return EnvRegistry(Path(directory) / "env.sqlite",
                       allowed_adapter_prefixes=("ghostlab.",) + extra_prefixes)


def register_refund(registry, env_id="refund-env", adapter_ref=REFUND_REF,
                    tools=None, modes=None, qualify=True):
    record = registry.register(
        env_id=env_id, env_version="refund-env/1", adapter_ref=adapter_ref,
        display_name="Synthetic refund",
        declared_tools=deepcopy(tools if tools is not None else REFUND_TOOLS),
        declared_modes=list(modes if modes is not None else REFUND_MODES))
    if qualify:
        # Execution refuses unqualified envs; qualify here so every test
        # starts from the executable state. Qualification is behavioral and
        # never consults declarations, so mismatch tests are unaffected.
        run_qualification(registry, env_id)
    return record


def make_store(directory):
    return ExperimentStore(Path(directory) / "experiments.sqlite")


def valid_policy(**overrides):
    policy = {"policy_version": "policy/1", "ceiling_tool_calls": 20,
              "ceiling_total_tool_calls": 200, "ceiling_repetitions": 5,
              "ceiling_wall_clock_ms": 60000,
              "allowed_agents": ["vulnerable", "repaired"],
              "allowed_hooks": ["timeout_after_commit"]}
    policy.update(overrides)
    return policy


def valid_requested(**overrides):
    obj = {"config_version": 1, "name": "refund timeout probe",
           "env_id": "refund-env", "agent": "vulnerable",
           "fixture": {"order_id": "synthetic-order-1",
                       "customer_id": "synthetic-customer-1",
                       "paid_minor": 250000, "currency": "INR"},
           "faults": [],
           "rules": ["authorized_refunds", "total_refund_within_payment",
                     "no_duplicate_full_refund", "unique_idempotency_keys"],
           "limits": {"max_tool_calls": 20, "max_total_tool_calls": 200,
                      "max_wall_clock_ms": 60000},
           "repetitions": {"count": 1}}
    obj.update(overrides)
    return obj


def faulted_requested(**overrides):
    obj = valid_requested()
    obj["faults"] = [{"hook": "timeout_after_commit", "params": {},
                      "required": True}]
    obj.update(overrides)
    return obj


def make_manifest(env_id="refund-env", **overrides):
    manifest = {"manifest_version": 1, "env_id": env_id,
                "env_version": "refund-env/1", "adapter_ref": REFUND_REF,
                "display_name": "Synthetic refund",
                "tools": deepcopy(REFUND_TOOLS),
                "reproduction_modes": list(REFUND_MODES),
                "fault_hooks": ["timeout_after_commit"],
                "notes": "reviewed local adapter"}
    manifest.update(overrides)
    return manifest


def counters():
    return (CountingAdapter.inits, CountingAdapter.describes,
            CountingAdapter.resets, CountingAdapter.invokes,
            CountingAdapter.faults)


class StrictRejectTests(unittest.TestCase):
    def check_rejected(self, obj, policy, fragment):
        with temp_dir() as directory:
            store = make_store(directory)
            with self.assertRaisesRegex(ValueError, fragment):
                ops.create_requested_config(store, "proj-a", obj, policy,
                                            "local-operator")
            self.assertEqual(store.list_requested("proj-a"), [])

    def test_unknown_top_level_field_rejected(self):
        obj = valid_requested(model_api_key="x")
        self.check_rejected(obj, valid_policy(), "exactly the v1 fields")

    def test_unknown_nested_fields_rejected(self):
        cases = [
            ("fixture", {"extra": "x"}),
            ("limits", {"max_tool_calls": 20, "max_total_tool_calls": 200,
                        "max_wall_clock_ms": 60000, "extra": 1}),
            ("repetitions", {"count": 1, "extra": 1}),
        ]
        for field, value in cases:
            with self.subTest(field=field):
                obj = valid_requested(**{field: value})
                self.check_rejected(obj, valid_policy(), field)
        obj = valid_requested(
            faults=[{"hook": "timeout_after_commit", "params": {},
                     "required": True, "extra": 1}])
        self.check_rejected(obj, valid_policy(), "faults")

    def test_bad_types_and_values_rejected(self):
        for obj in (valid_requested(config_version=True),
                    valid_requested(config_version=2),
                    valid_requested(name=""),
                    valid_requested(env_id="Refund!"),
                    valid_requested(agent="oracle"),
                    valid_requested(fixture={"order_id": "o",
                                             "customer_id": "c",
                                             "paid_minor": True,
                                             "currency": "INR"}),
                    valid_requested(rules=[]),
                    valid_requested(rules=["no_such_rule"]),
                    valid_requested(rules=["authorized_refunds",
                                           "authorized_refunds"]),
                    valid_requested(faults=[{"hook": "timeout_after_commit",
                                             "params": {"x": 1},
                                             "required": True}]),
                    valid_requested(faults=[
                        {"hook": "timeout_after_commit", "params": {},
                         "required": True},
                        {"hook": "timeout_after_commit", "params": {},
                         "required": False}])):
            with self.subTest(obj=obj):
                with temp_dir() as directory:
                    store = make_store(directory)
                    with self.assertRaises(ValueError):
                        ops.create_requested_config(store, "proj-a", obj,
                                                    valid_policy(),
                                                    "local-operator")

    def test_unsupported_agent_and_hook_rejected(self):
        self.check_rejected(valid_requested(agent="oracle"),
                            valid_policy(), "unsupported agent")
        self.check_rejected(
            valid_requested(), valid_policy(allowed_agents=["repaired"]),
            "not allowed by policy")
        obj = valid_requested(faults=[{"hook": "slow_network", "params": {},
                                       "required": True}])
        self.check_rejected(obj, valid_policy(), "unsupported fault hook")

    def test_policy_ceilings_reject_without_clamping(self):
        for obj, fragment in (
                (valid_requested(limits={"max_tool_calls": 21,
                                         "max_total_tool_calls": 200,
                                         "max_wall_clock_ms": 60000}),
                 "ceiling_tool_calls"),
                (valid_requested(limits={"max_tool_calls": 20,
                                         "max_total_tool_calls": 201,
                                         "max_wall_clock_ms": 60000}),
                 "ceiling_total_tool_calls"),
                (valid_requested(limits={"max_tool_calls": 20,
                                         "max_total_tool_calls": 200,
                                         "max_wall_clock_ms": 60001}),
                 "ceiling_wall_clock_ms"),
                (valid_requested(repetitions={"count": 6}),
                 "ceiling_repetitions")):
            with self.subTest(fragment=fragment):
                self.check_rejected(obj, valid_policy(), "ceiling")

    def test_impossible_combinations_rejected(self):
        self.check_rejected(
            valid_requested(limits={"max_tool_calls": 1,
                                    "max_total_tool_calls": 200,
                                    "max_wall_clock_ms": 60000}),
            valid_policy(), "at least get_order")
        self.check_rejected(
            valid_requested(limits={"max_tool_calls": 10,
                                    "max_total_tool_calls": 8,
                                    "max_wall_clock_ms": 60000}),
            valid_policy(), "exceeds max_total_tool_calls")
        self.check_rejected(
            valid_requested(limits={"max_tool_calls": 2,
                                    "max_total_tool_calls": 4,
                                    "max_wall_clock_ms": 60000},
                            repetitions={"count": 3}),
            valid_policy(), "impossible combination")

    def test_exact_ceiling_values_pass(self):
        with temp_dir() as directory:
            store = make_store(directory)
            record = ops.create_requested_config(
                store, "proj-a", valid_requested(), valid_policy(),
                "local-operator")
            self.assertEqual(record["config"]["limits"]["max_tool_calls"], 20)


class ResolveTests(unittest.TestCase):
    def test_pins_versions_effective_values_and_provenance(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            record = register_refund(registry)
            store = make_store(directory)
            created = ops.create_requested_config(
                store, "proj-a", faulted_requested(), valid_policy(),
                "local-operator")
            resolved = ops.resolve_config(
                store, registry, "proj-a", created["config_id"],
                valid_policy(), "local-operator")
            body = resolved["resolved"]
            self.assertEqual(body["pins"]["env_version"], "refund-env/1")
            self.assertEqual(body["pins"]["adapter_ref"], REFUND_REF)
            self.assertEqual(body["pins"]["code_digest"],
                             record["code_digest"])
            self.assertIsNone(body["pins"]["manifest_digest"])
            self.assertEqual(body["pins"]["world_version"], WORLD_VERSION)
            self.assertEqual(body["pins"]["adapter_version"], ADAPTER_VERSION)
            self.assertEqual(body["pins"]["controller_version"],
                             "experiment-controller/1")
            self.assertEqual(body["effective"]["agent"], "vulnerable")
            self.assertEqual(body["effective"]["fixture"],
                             faulted_requested()["fixture"])
            self.assertEqual(body["effective"]["faults"],
                             faulted_requested()["faults"])
            self.assertEqual(body["effective"]["seeds"], [0])
            self.assertEqual(body["provenance"]["policy_version"], "policy/1")
            self.assertEqual(body["provenance"]["requested_digest"],
                             created["digest"])
            self.assertTrue(body["provenance"]["discovery_match"])
            self.assertEqual(resolved["digest"], body["digest"])
            resolved_from_dict(body)  # digest verifies

    def test_resolve_is_idempotent(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            created = ops.create_requested_config(
                store, "proj-a", valid_requested(), valid_policy(),
                "local-operator")
            first = ops.resolve_config(store, registry, "proj-a",
                                       created["config_id"], valid_policy(),
                                       "local-operator")
            second = ops.resolve_config(store, registry, "proj-a",
                                        created["config_id"], valid_policy(),
                                        "local-operator")
            self.assertEqual(first["resolved_id"], second["resolved_id"])
            self.assertEqual(len(store.list_resolved("proj-a")), 1)

    def test_manifest_digest_pinned(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            summary = import_manifest(
                registry, json.dumps(make_manifest()).encode())
            store = make_store(directory)
            created = ops.create_requested_config(
                store, "proj-a", valid_requested(), valid_policy(),
                "local-operator")
            resolved = ops.resolve_config(
                store, registry, "proj-a", created["config_id"],
                valid_policy(), "local-operator",
                manifest_digest=summary["manifest_digest"])
            self.assertEqual(resolved["resolved"]["pins"]["manifest_digest"],
                             summary["manifest_digest"])

    def test_unknown_env_rejected(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            store = make_store(directory)
            created = ops.create_requested_config(
                store, "proj-a", valid_requested(env_id="ghost-env"),
                valid_policy(), "local-operator")
            with self.assertRaisesRegex(ValueError, "unknown env_id"):
                ops.resolve_config(store, registry, "proj-a",
                                   created["config_id"], valid_policy(),
                                   "local-operator")

    def test_declaration_mismatch_rejected(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(
                registry, tools=[{"name": "get_order",
                                  "parameters": ["order_id", "customer_id"]}])
            store = make_store(directory)
            created = ops.create_requested_config(
                store, "proj-a", valid_requested(), valid_policy(),
                "local-operator")
            with self.assertRaisesRegex(ValueError, "mismatch"):
                ops.resolve_config(store, registry, "proj-a",
                                   created["config_id"], valid_policy(),
                                   "local-operator")

    def test_undiscovered_hook_rejected(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            policy = valid_policy(
                allowed_hooks=["timeout_after_commit", "slow_network"])
            obj = valid_requested(faults=[{"hook": "slow_network",
                                           "params": {}, "required": True}])
            created = ops.create_requested_config(store, "proj-a", obj,
                                                  policy, "local-operator")
            with self.assertRaisesRegex(ValueError, "not implemented"):
                ops.resolve_config(store, registry, "proj-a",
                                   created["config_id"], policy,
                                   "local-operator")

    def test_missing_required_tool_rejected(self):
        with temp_dir() as directory:
            registry = make_registry(directory, __name__)
            register_refund(
                registry, adapter_ref=f"{__name__}:SlimAdapter",
                tools=[{"name": "get_order",
                        "parameters": ["order_id", "customer_id"]}])
            store = make_store(directory)
            created = ops.create_requested_config(
                store, "proj-a", valid_requested(),
                valid_policy(), "local-operator")
            with self.assertRaisesRegex(ValueError, "needs tool"):
                ops.resolve_config(store, registry, "proj-a",
                                   created["config_id"], valid_policy(),
                                   "local-operator")


class PurityTests(unittest.TestCase):
    def test_validation_needs_no_registry_or_inference(self):
        # Pure functions of their inputs: no registry exists here at all.
        policy = validate_policy(valid_policy())
        self.assertEqual(policy["policy_version"], "policy/1")
        body = validate_requested(valid_requested(), valid_policy())
        self.assertEqual(body["agent"], "vulnerable")
        requested = RequestedConfig.from_obj(valid_requested(),
                                             valid_policy())
        self.assertEqual(requested.to_dict(), body)

    def test_resolve_is_describe_only(self):
        with temp_dir() as directory:
            registry = make_registry(directory, __name__)
            register_refund(
                registry, adapter_ref=f"{__name__}:CountingAdapter")
            store = make_store(directory)
            created = ops.create_requested_config(
                store, "proj-a", faulted_requested(),
                valid_policy(), "local-operator")
            CountingAdapter.reset_counters()
            ops.resolve_config(store, registry, "proj-a",
                               created["config_id"], valid_policy(),
                               "local-operator")
            self.assertEqual(counters(), (1, 1, 0, 0, 0))

    def test_runs_record_zero_model_calls(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            created = ops.create_requested_config(
                store, "proj-a", faulted_requested(agent="repaired"),
                valid_policy(), "local-operator")
            resolved = ops.resolve_config(store, registry, "proj-a",
                                          created["config_id"],
                                          valid_policy(), "local-operator")
            run = ops.start_run(store, registry, "proj-a",
                                resolved["resolved_id"])
            self.assertEqual(run["run"]["model_calls"], 0)


class CrossProjectTests(unittest.TestCase):
    def test_cross_project_access_refused(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            created = ops.create_requested_config(
                store, "proj-a", valid_requested(), valid_policy(),
                "local-operator")
            resolved = ops.resolve_config(store, registry, "proj-a",
                                          created["config_id"],
                                          valid_policy(), "local-operator")
            run = ops.start_run(store, registry, "proj-a",
                                resolved["resolved_id"])
            for thunk in (
                    lambda: ops.get_requested_config(
                        store, "proj-b", created["config_id"]),
                    lambda: ops.get_resolved_config(
                        store, "proj-b", resolved["resolved_id"]),
                    lambda: ops.get_run(store, "proj-b", run["run_id"]),
                    lambda: ops.resolve_config(
                        store, registry, "proj-b", created["config_id"],
                        valid_policy(), "local-operator"),
                    lambda: ops.start_run(store, registry, "proj-b",
                                          resolved["resolved_id"]),
                    lambda: ops.compare_resolved(
                        store, "proj-b", resolved["resolved_id"],
                        resolved["resolved_id"]),
                    lambda: ops.budget_used(store, "proj-b",
                                            resolved["resolved_id"])):
                with self.assertRaises(CrossProjectError):
                    thunk()
            self.assertEqual(ops.list_requested_configs(store, "proj-b"),
                             {"project_id": "proj-b", "configs": []})
            self.assertEqual(ops.list_resolved_configs(store, "proj-b"),
                             {"project_id": "proj-b", "configs": []})
            self.assertEqual(ops.list_runs(store, "proj-b"),
                             {"project_id": "proj-b", "runs": []})

    def test_unknown_ids_are_not_cross_project(self):
        with temp_dir() as directory:
            store = make_store(directory)
            with self.assertRaises(ValueError):
                ops.get_requested_config(store, "proj-a", "req-missing")


class SecretCanaryTests(unittest.TestCase):
    def test_secret_smuggling_rejected_and_never_echoed(self):
        smuggled = valid_requested(model_api_key="MODEL_API_KEY=" + CANARY)
        with temp_dir() as directory:
            store = make_store(directory)
            with self.assertRaises(ValueError) as raised:
                ops.create_requested_config(store, "proj-a", smuggled,
                                            valid_policy(), "local-operator")
            self.assertNotIn(CANARY, str(raised.exception))
            sneaky = valid_requested(name="probe MODEL_API_KEY=" + CANARY)
            with self.assertRaises(ValueError) as raised:
                ops.create_requested_config(store, "proj-a", sneaky,
                                            valid_policy(), "local-operator")
            self.assertNotIn(CANARY, str(raised.exception))

    def test_canary_absent_from_storage_and_outputs(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            created = ops.create_requested_config(
                store, "proj-a", faulted_requested(), valid_policy(),
                "local-operator")
            resolved = ops.resolve_config(store, registry, "proj-a",
                                          created["config_id"],
                                          valid_policy(), "local-operator")
            run = ops.start_run(store, registry, "proj-a",
                                resolved["resolved_id"])
            for path in (Path(directory) / "experiments.sqlite",
                         Path(directory) / "env.sqlite"):
                self.assertNotIn(CANARY.encode(), path.read_bytes())
                self.assertNotIn(b"API_KEY", path.read_bytes())
            blob = json.dumps([created, resolved, run])
            self.assertNotIn(CANARY, blob)


class AggregateBudgetTests(unittest.TestCase):
    def test_envelope_covers_repetitions_then_refuses(self):
        with temp_dir() as directory:
            registry = make_registry(directory, __name__)
            register_refund(
                registry, adapter_ref=f"{__name__}:CountingAdapter")
            store = make_store(directory)
            obj = valid_requested(
                limits={"max_tool_calls": 2, "max_total_tool_calls": 4,
                        "max_wall_clock_ms": 60000},
                repetitions={"count": 2})
            created = ops.create_requested_config(store, "proj-a", obj,
                                                  valid_policy(),
                                                  "local-operator")
            resolved = ops.resolve_config(store, registry, "proj-a",
                                          created["config_id"],
                                          valid_policy(), "local-operator")
            CountingAdapter.reset_counters()
            first = ops.start_run(store, registry, "proj-a",
                                  resolved["resolved_id"], repetition=0,
                                  attempt=0)
            second = ops.start_run(store, registry, "proj-a",
                                   resolved["resolved_id"], repetition=1,
                                   attempt=0)
            self.assertEqual(first["run"]["tool_calls"], 2)
            self.assertEqual(second["run"]["tool_calls"], 2)
            self.assertEqual(ops.budget_used(store, "proj-a",
                                             resolved["resolved_id"])["used"], 4)
            before = counters()
            with self.assertRaisesRegex(BudgetExhaustedError,
                                        "envelope exhausted"):
                ops.start_run(store, registry, "proj-a",
                              resolved["resolved_id"], repetition=0, attempt=1)
            self.assertEqual(counters(), before)

    def test_budgets_survive_retries_and_recovery(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            obj = faulted_requested(
                limits={"max_tool_calls": 3, "max_total_tool_calls": 5,
                        "max_wall_clock_ms": 60000},
                repetitions={"count": 2})
            created = ops.create_requested_config(store, "proj-a", obj,
                                                  valid_policy(),
                                                  "local-operator")
            resolved = ops.resolve_config(store, registry, "proj-a",
                                          created["config_id"],
                                          valid_policy(), "local-operator")
            first = ops.start_run(store, registry, "proj-a",
                                  resolved["resolved_id"], repetition=0,
                                  attempt=0)
            self.assertEqual(first["run"]["status"], "completed")
            retry = ops.start_run(store, registry, "proj-a",
                                  resolved["resolved_id"], repetition=0,
                                  attempt=1)
            self.assertEqual(retry["run"]["status"], "budget_exhausted")
            self.assertEqual(retry["run"]["tool_calls"], 2)
            self.assertEqual(retry["run"]["verdict"], "incomplete")
            self.assertEqual(ops.budget_used(store, "proj-a",
                                             resolved["resolved_id"])["used"], 5)
            with self.assertRaises(BudgetExhaustedError):
                ops.start_run(store, registry, "proj-a",
                              resolved["resolved_id"], repetition=1, attempt=0)
            reopened = ExperimentStore(Path(directory) / "experiments.sqlite")
            self.assertEqual(reopened.budget_used("proj-a",
                                                 resolved["resolved_id"]), 5)

    def test_attempt_numbers_are_single_use(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            created = ops.create_requested_config(
                store, "proj-a", valid_requested(), valid_policy(),
                "local-operator")
            resolved = ops.resolve_config(store, registry, "proj-a",
                                          created["config_id"],
                                          valid_policy(), "local-operator")
            ops.start_run(store, registry, "proj-a", resolved["resolved_id"],
                          repetition=0, attempt=0)
            with self.assertRaisesRegex(ValueError, "already recorded"):
                ops.start_run(store, registry, "proj-a",
                              resolved["resolved_id"], repetition=0, attempt=0)


class FaultVerdictTests(unittest.TestCase):
    def run_faulted(self, directory, agent):
        registry = make_registry(directory)
        register_refund(registry)
        store = make_store(directory)
        created = ops.create_requested_config(
            store, "proj-a", faulted_requested(agent=agent), valid_policy(),
            "local-operator")
        resolved = ops.resolve_config(store, registry, "proj-a",
                                      created["config_id"], valid_policy(),
                                      "local-operator")
        run = ops.start_run(store, registry, "proj-a",
                            resolved["resolved_id"])
        return run

    def test_required_fault_fired_and_pass(self):
        with temp_dir() as directory:
            run = self.run_faulted(directory, "repaired")
            self.assertEqual(run["run"]["status"], "completed")
            self.assertEqual(run["run"]["faults"],
                             [{"hook": "timeout_after_commit",
                               "required": True, "fired": True}])
            self.assertEqual(run["run"]["verdict"], "pass")

    def test_rule_failure_is_fail(self):
        with temp_dir() as directory:
            run = self.run_faulted(directory, "vulnerable")
            self.assertEqual(run["run"]["status"], "completed")
            self.assertIn("total_refund_within_payment",
                          run["run"]["rules_failed"])
            self.assertEqual(run["run"]["verdict"], "fail")

    def test_cancelled_run_records_never_fired_and_incomplete(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            created = ops.create_requested_config(
                store, "proj-a", faulted_requested(), valid_policy(),
                "local-operator")
            resolved = ops.resolve_config(store, registry, "proj-a",
                                          created["config_id"],
                                          valid_policy(), "local-operator")
            cancel = threading.Event()
            cancel.set()
            run = ops.start_run(store, registry, "proj-a",
                                resolved["resolved_id"], cancel=cancel)
            self.assertEqual(run["run"]["status"], "cancelled")
            self.assertEqual(run["run"]["tool_calls"], 0)
            self.assertEqual(run["run"]["faults"],
                             [{"hook": "timeout_after_commit",
                               "required": True, "fired": False}])
            self.assertEqual(run["run"]["verdict"], "incomplete")

    def test_budget_exhaustion_is_incomplete_not_pass(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            obj = faulted_requested(
                limits={"max_tool_calls": 2, "max_total_tool_calls": 200,
                        "max_wall_clock_ms": 60000})
            created = ops.create_requested_config(store, "proj-a", obj,
                                                  valid_policy(),
                                                  "local-operator")
            resolved = ops.resolve_config(store, registry, "proj-a",
                                          created["config_id"],
                                          valid_policy(), "local-operator")
            run = ops.start_run(store, registry, "proj-a",
                                resolved["resolved_id"])
            self.assertEqual(run["run"]["status"], "budget_exhausted")
            self.assertEqual(run["run"]["verdict"], "incomplete")
            self.assertTrue(run["run"]["faults"][0]["fired"])

    def test_evaluate_verdict_matrix(self):
        checks = {"authorized_refunds": True,
                  "total_refund_within_payment": True,
                  "no_duplicate_full_refund": True,
                  "unique_idempotency_keys": True}
        fired = [{"hook": "timeout_after_commit", "required": True,
                  "fired": True}]
        self.assertEqual(evaluate_verdict(
            status="completed", evidence_ok=True, checks=checks,
            rules=list(checks), faults=fired), "pass")
        self.assertEqual(evaluate_verdict(
            status="completed", evidence_ok=True,
            checks={**checks, "total_refund_within_payment": False},
            rules=list(checks), faults=fired), "fail")
        self.assertEqual(evaluate_verdict(
            status="completed", evidence_ok=True, checks=checks,
            rules=list(checks),
            faults=[{"hook": "timeout_after_commit", "required": True,
                     "fired": False}]), "fail")
        for status, ok in (("completed", False), ("budget_exhausted", True),
                           ("cancelled", True), ("error", True)):
            self.assertEqual(evaluate_verdict(
                status=status, evidence_ok=ok, checks=checks,
                rules=list(checks), faults=fired), "incomplete")
        self.assertEqual(evaluate_verdict(
            status="completed", evidence_ok=True, checks={},
            rules=list(checks), faults=fired), "fail")

    def test_fault_and_evidence_helpers(self):
        trace = [{"event_id": 1, "request": {}, "outcome": {"result": {}},
                  "state": {}},
                 {"event_id": 2, "request": {},
                  "outcome": {"error": "PaymentTimeout", "message": "t"},
                  "state": {}}]
        self.assertTrue(fault_fired("timeout_after_commit", trace))
        self.assertFalse(fault_fired("timeout_after_commit", trace[:1]))
        self.assertFalse(fault_fired("slow_network", trace))
        self.assertFalse(fault_fired("timeout_after_commit", None))
        evidence = {"snapshot": {"order": {}, "ledger": [],
                                 "total_refunded_minor": 0},
                    "trace": trace}
        self.assertTrue(evidence_complete(evidence, 2))
        self.assertFalse(evidence_complete(evidence, 1))
        self.assertFalse(evidence_complete({"trace": []}, 0))
        broken = deepcopy(evidence)
        broken["snapshot"]["total_refunded_minor"] = 1
        self.assertFalse(evidence_complete(broken, 2))


class RefundPreservationTests(unittest.TestCase):
    def execute(self, directory, obj):
        registry = make_registry(directory, __name__)
        try:
            register_refund(registry,
                            adapter_ref=f"{__name__}:CountingAdapter")
        except ValueError as error:
            if "already registered" not in str(error):
                raise
        store = make_store(directory)
        created = ops.create_requested_config(store, "proj-a", obj,
                                              valid_policy(), "local-operator")
        resolved = ops.resolve_config(store, registry, "proj-a",
                                      created["config_id"], valid_policy(),
                                      "local-operator")
        return ops.start_run(store, registry, "proj-a",
                             resolved["resolved_id"])

    @staticmethod
    def as_core_run(run):
        return {"trace": run["run"]["evidence"]["trace"],
                "final_state": run["run"]["evidence"]["snapshot"],
                "checks": run["run"]["checks"]}

    def test_baseline_matches_core(self):
        with temp_dir() as directory:
            run = self.execute(directory, valid_requested())
            expected = core_run(Fixture(), Scenario(), "vulnerable")
            self.assertEqual(run["run"]["evidence"]["trace"],
                             expected["trace"])
            self.assertEqual(run["run"]["evidence"]["snapshot"],
                             expected["final_state"])
            self.assertEqual(run["run"]["checks"], expected["checks"])
            self.assertEqual(run["run"]["verdict"], "pass")

    def test_faulted_and_repaired_match_core(self):
        with temp_dir() as directory:
            faulted = self.execute(directory, faulted_requested())
            expected_faulted = core_run(
                Fixture(), Scenario(timeout_after_commit=True), "vulnerable")
            self.assertEqual(faulted["run"]["evidence"]["trace"],
                             expected_faulted["trace"])
            self.assertEqual(faulted["run"]["evidence"]["snapshot"],
                             expected_faulted["final_state"])
            self.assertEqual(
                faulted["run"]["evidence"]["snapshot"]
                ["total_refunded_minor"], 500000)
            repaired = self.execute(
                directory, faulted_requested(agent="repaired"))
            expected_repaired = core_run(
                Fixture(), Scenario(timeout_after_commit=True), "repaired")
            self.assertEqual(repaired["run"]["evidence"]["trace"],
                             expected_repaired["trace"])
            self.assertEqual(
                repaired["run"]["evidence"]["snapshot"]
                ["total_refunded_minor"], 250000)
            self.assertTrue(all(repaired["run"]["checks"].values()))

    def test_reality_diff_matches_core(self):
        with temp_dir() as directory:
            baseline = self.as_core_run(self.execute(
                directory, valid_requested()))
            faulted = self.as_core_run(self.execute(
                directory, faulted_requested()))
            diff = reality_diff(baseline, faulted)
            self.assertEqual(diff["first_divergence"]["event_id"], 2)
            self.assertEqual(diff["baseline_refunded_minor"], 250000)
            self.assertEqual(diff["faulted_refunded_minor"], 500000)


class DiffTests(unittest.TestCase):
    def resolve(self, store, registry, obj):
        created = ops.create_requested_config(store, "proj-a", obj,
                                              valid_policy(), "local-operator")
        return ops.resolve_config(store, registry, "proj-a",
                                  created["config_id"], valid_policy(),
                                  "local-operator")

    def test_identical_compare_equal(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            resolved = self.resolve(store, registry, valid_requested())
            compared = ops.compare_resolved(store, "proj-a",
                                            resolved["resolved_id"],
                                            resolved["resolved_id"])
            self.assertTrue(compared["equal"])
            self.assertEqual(compared["diffs"], [])

    def test_agent_and_limit_differences(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            left = self.resolve(store, registry, valid_requested())
            right = self.resolve(
                store, registry,
                valid_requested(agent="repaired",
                                limits={"max_tool_calls": 10,
                                        "max_total_tool_calls": 100,
                                        "max_wall_clock_ms": 60000}))
            compared = ops.compare_resolved(store, "proj-a",
                                            left["resolved_id"],
                                            right["resolved_id"])
            self.assertFalse(compared["equal"])
            by_path = {d["path"]: d for d in compared["diffs"]}
            self.assertEqual(by_path["effective.agent"]["left"], "vulnerable")
            self.assertEqual(by_path["effective.agent"]["right"], "repaired")
            self.assertEqual(
                by_path["effective.limits.max_tool_calls"]["left"], 20)
            self.assertEqual(
                by_path["effective.limits.max_tool_calls"]["right"], 10)

    def test_identity_fields_are_skipped(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            resolved = self.resolve(store, registry, valid_requested())
            twin = deepcopy(resolved["resolved"])
            twin["resolved_config_id"] = "res-other"
            twin["resolved_at"] = "2030-01-01T00:00:00Z"
            twin["digest"] = "0" * 64
            compared = diff_resolved(resolved["resolved"], twin)
            self.assertTrue(compared["equal"])


class PersistenceTests(unittest.TestCase):
    def test_roundtrip_and_recovery(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            created = ops.create_requested_config(
                store, "proj-a", faulted_requested(), valid_policy(),
                "local-operator")
            resolved = ops.resolve_config(store, registry, "proj-a",
                                          created["config_id"],
                                          valid_policy(), "local-operator")
            run = ops.start_run(store, registry, "proj-a",
                                resolved["resolved_id"])
            reopened = ExperimentStore(Path(directory) / "experiments.sqlite")
            self.assertEqual(reopened.get_requested("proj-a",
                                                   created["config_id"]),
                             created)
            self.assertEqual(reopened.get_resolved("proj-a",
                                                  resolved["resolved_id"]),
                             resolved)
            self.assertEqual(reopened.get_run("proj-a", run["run_id"]), run)
            self.assertEqual(len(reopened.list_runs("proj-a")), 1)

    def test_returned_records_do_not_alias_storage(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            created = ops.create_requested_config(
                store, "proj-a", valid_requested(), valid_policy(),
                "local-operator")
            created["config"]["name"] = "mutated"
            fresh = ops.get_requested_config(store, "proj-a",
                                             created["config_id"])
            self.assertEqual(fresh["config"]["name"], "refund timeout probe")

    def test_tampered_storage_fails_cleanly(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            store = make_store(directory)
            created = ops.create_requested_config(
                store, "proj-a", valid_requested(), valid_policy(),
                "local-operator")
            resolved = ops.resolve_config(store, registry, "proj-a",
                                          created["config_id"],
                                          valid_policy(), "local-operator")
            connection = sqlite3.connect(Path(directory) / "experiments.sqlite")
            try:
                row = connection.execute(
                    "SELECT resolved_json FROM resolved_configs"
                    " WHERE resolved_id = ?",
                    (resolved["resolved_id"],)).fetchone()
                body = json.loads(row[0])
                body["effective"]["agent"] = "repaired"
                connection.execute(
                    "UPDATE resolved_configs SET resolved_json = ?"
                    " WHERE resolved_id = ?",
                    (json.dumps(body), resolved["resolved_id"]))
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                ops.get_resolved_config(store, "proj-a",
                                        resolved["resolved_id"])


if __name__ == "__main__":
    unittest.main()
