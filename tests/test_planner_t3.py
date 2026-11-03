"""Track T3: exploration planner + coverage + fidelity report.

Every test uses track-private temporary databases (a fresh experiment store
plus a fresh environment registry per test, under /tmp when it exists);
no live registry file is touched. All execution runs against refund-env,
qualified in-test. Zero model calls.
"""

import contextlib
import gc
import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from ghostlab.envs.adapters import (REFUND_MODES, REFUND_TOOLS,
                                    RefundEnvAdapter)
from ghostlab.envs.qualification import run_qualification
from ghostlab.envs.registry import EnvRegistry
from ghostlab.experiments import ops
from ghostlab.experiments.plan import (PlanError, ScriptedExplorer,
                                       build_plan, execute_plan, expand_plan)
from ghostlab.experiments.store import ExperimentStore
from ghostlab.fidelity.diverge import compare_runs, fidelity_probe
from ghostlab.fidelity.manifests import REFUND_FIDELITY_MANIFEST
from ghostlab.fidelity.report import assemble_fidelity_report

REFUND_REF = "ghostlab.envs.adapters:RefundEnvAdapter"
PROJECT = "t3-proj"

FIXTURE = {"order_id": "synthetic-order-1",
           "customer_id": "synthetic-customer-1",
           "paid_minor": 250000, "currency": "INR"}
LIMITS = {"max_tool_calls": 20, "max_total_tool_calls": 200,
          "max_wall_clock_ms": 60000}
RULES = ["authorized_refunds", "total_refund_within_payment",
         "no_duplicate_full_refund", "unique_idempotency_keys"]


@contextlib.contextmanager
def temp_dir():
    base = "/tmp" if os.path.isdir("/tmp") else None
    with tempfile.TemporaryDirectory(prefix="track-t3-", dir=base) as directory:
        yield directory
        gc.collect()


def make_registry(directory):
    return EnvRegistry(Path(directory) / "env.sqlite",
                       allowed_adapter_prefixes=("ghostlab.",))


def register_refund(registry):
    registry.register(
        env_id="refund-env", env_version="refund-env/1",
        adapter_ref=REFUND_REF, display_name="Synthetic refund",
        declared_tools=deepcopy(REFUND_TOOLS),
        declared_modes=list(REFUND_MODES))
    run_qualification(registry, "refund-env")


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


def valid_requested(name="t3 probe", **overrides):
    obj = {"config_version": 1, "name": name, "env_id": "refund-env",
           "agent": "vulnerable", "fixture": deepcopy(FIXTURE),
           "faults": [], "rules": list(RULES), "limits": dict(LIMITS),
           "repetitions": {"count": 1}}
    obj.update(overrides)
    return obj


def faulted_requested(name="t3 faulted", **overrides):
    obj = valid_requested(name)
    obj["faults"] = [{"hook": "timeout_after_commit", "params": {},
                      "required": True}]
    obj.update(overrides)
    return obj


def triggered_requested(name="t3 triggered", occurrence=1,
                        tool="issue_refund", **overrides):
    obj = valid_requested(name)
    obj["faults"] = [{"hook": "timeout_after_commit", "params": {},
                      "required": True,
                      "trigger": {"type": "occurrence", "tool": tool,
                                  "occurrence": occurrence}}]
    obj.update(overrides)
    return obj


class FixedPlanTests(unittest.TestCase):
    def test_fixed_plan_exact_count(self):
        first = valid_requested("first")
        second = valid_requested("second", repetitions={"count": 2})
        plan = build_plan("refund-env", FIXTURE, "vulnerable", "probe",
                          RULES, None, LIMITS, 1, "fixed", [0],
                          requested_configs=[first, second],
                          policy=valid_policy())
        expanded = expand_plan(plan)
        self.assertEqual(expanded["report"]["planned_run_count"], 3)
        self.assertEqual(expanded["report"]["planned_configs"], 2)
        units = expanded["units"]
        self.assertEqual(len(units), 3)
        self.assertEqual((units[0][1], units[0][2]), (0, 0))
        self.assertEqual(units[0][0]["name"], "first")
        self.assertEqual((units[1][1], units[1][2]), (0, 0))
        self.assertEqual((units[2][1], units[2][2]), (1, 0))
        self.assertEqual(units[2][0]["name"], "second")

    def test_fixed_executes_baseline_pass(self):
        with temp_dir() as directory:
            registry, store = make_registry(directory), make_store(directory)
            register_refund(registry)
            plan = build_plan("refund-env", FIXTURE, "vulnerable", "probe",
                              RULES, None, LIMITS, 1, "fixed", [0],
                              requested_configs=[valid_requested()],
                              policy=valid_policy())
            result = execute_plan(store, registry, plan, PROJECT)
        self.assertEqual(result["runs_executed"], 1)
        self.assertEqual(result["per_run"][0]["status"], "completed")
        self.assertEqual(result["per_run"][0]["verdict"], "pass")
        self.assertTrue(result["passed"])
        self.assertEqual(result["violations"], [])
        self.assertEqual(result["incompletes"], [])
        self.assertTrue(result["all_required_tested"])
        self.assertGreater(result["usage"]["tool_calls_total"], 0)


class RandomPlanTests(unittest.TestCase):
    def _space(self, **overrides):
        space = {"hooks": ["timeout_after_commit"],
                 "tools": ["issue_refund", "*"], "occurrences": [1, 2],
                 "count": 3, "required": True}
        space.update(overrides)
        return space

    def test_seeded_random_deterministic(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            plan = build_plan("refund-env", FIXTURE, "vulnerable", "probe",
                              RULES, self._space(), LIMITS, 2,
                              "stateful_random", [7], policy=valid_policy())
            first = expand_plan(plan, registry)
            second = expand_plan(plan, registry)
        self.assertEqual(first["report"]["planned_run_count"], 6)
        self.assertEqual(json.dumps(first["units"], sort_keys=True,
                                    default=str),
                         json.dumps(second["units"], sort_keys=True,
                                    default=str))

    def test_unsupported_hook_never_generated(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            space = self._space(
                hooks=["timeout_after_commit", "no_such_hook_xyz"])
            plan = build_plan("refund-env", FIXTURE, "vulnerable", "probe",
                              RULES, space, LIMITS, 1, "stateful_random",
                              [7], policy=valid_policy())
            expanded = expand_plan(plan, registry)
        flagged = [c for c in
                   expanded["report"]["unsupported_conditions"]
                   if c["value"] == "no_such_hook_xyz"]
        self.assertTrue(flagged)
        for body, _, _ in expanded["units"]:
            hooks = [f["hook"] for f in body["faults"]]
            self.assertNotIn("no_such_hook_xyz", hooks)
            self.assertIn("timeout_after_commit", hooks)

    def test_random_needs_registry(self):
        plan = build_plan("refund-env", FIXTURE, "vulnerable", "probe",
                          RULES, self._space(), LIMITS, 1, "stateful_random",
                          [7], policy=valid_policy())
        with self.assertRaises(PlanError):
            expand_plan(plan)


class GuidedPlanTests(unittest.TestCase):
    def test_guided_invalid_rejected_valid_executes(self):
        valid = valid_requested("guided valid")
        invalid = valid_requested("guided invalid",
                                  rules=["not_a_rule_xyz"])
        explorer = ScriptedExplorer([valid, invalid, "junk-proposal"])
        plan = build_plan("refund-env", FIXTURE, "vulnerable", "probe",
                          RULES, None, LIMITS, 1, "model_guided", [0],
                          explorer=explorer, policy=valid_policy())
        expanded = expand_plan(plan)
        self.assertEqual(len(expanded["report"]["rejected_proposals"]), 1)
        with temp_dir() as directory:
            registry, store = make_registry(directory), make_store(directory)
            register_refund(registry)
            result = execute_plan(store, registry, plan, PROJECT)
            listed = ops.list_runs(store, PROJECT)
        self.assertEqual(result["runs_executed"], 1)
        self.assertEqual(len(result["rejected"]), 2)
        stages = sorted(r["stage"] for r in result["rejected"])
        self.assertEqual(stages, ["create", "expand"])
        self.assertEqual(len(listed["runs"]), 1)

    def test_guided_needs_explorer(self):
        with self.assertRaises(PlanError):
            build_plan("refund-env", FIXTURE, "vulnerable", "probe",
                       RULES, None, LIMITS, 1, "model_guided", [0],
                       policy=valid_policy())


class CoverageReportTests(unittest.TestCase):
    def test_preexecution_counts_coverage_unsupported(self):
        with temp_dir() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            body = triggered_requested()
            plan = build_plan("refund-env", FIXTURE, "vulnerable", "probe",
                              RULES, None, LIMITS, 1, "fixed", [0],
                              requested_configs=[body],
                              policy=valid_policy())
            expanded = expand_plan(plan, registry)
        report = expanded["report"]
        self.assertEqual(report["planned_run_count"], 1)
        self.assertEqual(report["supported_source"], "discovered")
        coverage = report["coverage"]
        self.assertIn("timeout_after_commit", coverage["hooks"])
        self.assertIn("issue_refund", coverage["tools"])
        hit = [c for c in coverage["cells"]
               if c["hook"] == "timeout_after_commit"
               and c["tool"] == "issue_refund" and c["occurrence"] == 1]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["planned_runs"], 1)
        self.assertGreaterEqual(coverage["total_cells"],
                                coverage["covered_cells"])
        self.assertGreaterEqual(coverage["covered_cells"], 1)
        self.assertEqual(report["unsupported_conditions"], [])

    def test_fixed_malformed_body_rejected_preexecution(self):
        plan = build_plan("refund-env", FIXTURE, "vulnerable", "probe",
                          RULES, None, LIMITS, 1, "fixed", [0],
                          requested_configs=[{"name": "shapeless"}],
                          policy=valid_policy())
        with self.assertRaises(PlanError):
            expand_plan(plan)


class ExecuteSemanticsTests(unittest.TestCase):
    def test_unreached_required_stays_untested(self):
        with temp_dir() as directory:
            registry, store = make_registry(directory), make_store(directory)
            register_refund(registry)
            body = triggered_requested(occurrence=60)
            plan = build_plan("refund-env", FIXTURE, "vulnerable", "probe",
                              RULES, None, LIMITS, 1, "fixed", [0],
                              requested_configs=[body],
                              policy=valid_policy())
            result = execute_plan(store, registry, plan, PROJECT)
        self.assertEqual(result["per_run"][0]["verdict"], "fail")
        self.assertFalse(result["all_required_tested"])
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["untested_required"]), 1)
        untested = result["untested_required"][0]
        self.assertEqual(untested["target"], "timeout_after_commit")
        self.assertEqual(untested["outcome"], "not-reached")
        fault_rows = [f for f in result["per_fault"]
                      if f["required"]]
        self.assertTrue(all(f["outcome"] == "not-reached"
                            for f in fault_rows))

    def test_violation_and_incomplete_both_recorded(self):
        with temp_dir() as directory:
            registry, store = make_registry(directory), make_store(directory)
            register_refund(registry)
            violated = faulted_requested("violated")
            starved = faulted_requested(
                "starved", limits={**LIMITS, "max_tool_calls": 2})
            plan = build_plan("refund-env", FIXTURE, "vulnerable", "probe",
                              RULES, None, LIMITS, 1, "fixed", [0],
                              requested_configs=[violated, starved],
                              policy=valid_policy())
            result = execute_plan(store, registry, plan, PROJECT)
        self.assertEqual(result["runs_executed"], 2)
        self.assertEqual(len(result["violations"]), 1)
        self.assertTrue(result["violations"][0]["rules_failed"])
        self.assertEqual(len(result["incompletes"]), 1)
        self.assertNotEqual(result["incompletes"][0]["status"], "completed")
        self.assertFalse(result["passed"])

    def test_heldout_execution_refused(self):
        plan = build_plan("refund-env", FIXTURE, "vulnerable", "probe",
                          RULES, None, LIMITS, 1, "fixed", [0],
                          requested_configs=[valid_requested()],
                          policy=valid_policy(), source="heldout")
        expanded = expand_plan(plan)
        self.assertEqual(expanded["report"]["source"], "heldout")
        with temp_dir() as directory:
            registry, store = make_registry(directory), make_store(directory)
            register_refund(registry)
            with self.assertRaises(PlanError) as raised:
                execute_plan(store, registry, plan, PROJECT)
            self.assertIn("heldout", str(raised.exception).lower())
            listed = ops.list_runs(store, PROJECT)
        self.assertEqual(listed["runs"], [])


class FidelityReportTests(unittest.TestCase):
    def _evidence(self):
        fixture = deepcopy(FIXTURE)
        actions = [{"tool": "get_order",
                    "arguments": {"order_id": fixture["order_id"],
                                  "customer_id": fixture["customer_id"]}}]
        comparison = compare_runs(RefundEnvAdapter, RefundEnvAdapter,
                                  fixture, actions)
        probe = fidelity_probe(RefundEnvAdapter, fixture, actions)
        return comparison, probe

    def test_report_assembles_without_score(self):
        comparison, probe = self._evidence()
        report = assemble_fidelity_report(
            "refund-env", manifest=deepcopy(REFUND_FIDELITY_MANIFEST),
            comparisons=[comparison], probes=[probe],
            notes=["assessed from recorded evidence only"],
            behaviors_modeled=["refund commit then timeout"],
            approximated=["at-least-once retry timing"],
            unsupported_ops=["partial refunds"],
            unsupported_faults=["permission_revoked"])
        self.assertEqual(report["env_id"], "refund-env")
        self.assertEqual(report["assessed_by"],
                         REFUND_FIDELITY_MANIFEST["assessed_by"])
        self.assertEqual(report["assessed_at"],
                         REFUND_FIDELITY_MANIFEST["assessed_at"])
        self.assertEqual(len(report["comparisons"]), 1)
        self.assertEqual(len(report["determinism"]), 1)
        self.assertTrue(report["determinism"][0]["deterministic"])
        self.assertEqual(report["behaviors_modeled"],
                         ["refund commit then timeout"])
        self.assertNotIn("score", json.dumps(report).lower())

    def test_report_rejects_unknown_fields(self):
        comparison, probe = self._evidence()
        bad_comparison = dict(comparison, extra_field=1)
        with self.assertRaises(ValueError):
            assemble_fidelity_report(
                "refund-env",
                manifest=deepcopy(REFUND_FIDELITY_MANIFEST),
                comparisons=[bad_comparison])
        bad_probe = {k: v for k, v in probe.items()
                     if k != "deterministic"}
        with self.assertRaises(ValueError):
            assemble_fidelity_report(
                "refund-env",
                manifest=deepcopy(REFUND_FIDELITY_MANIFEST),
                probes=[bad_probe])
        with self.assertRaises(TypeError):
            assemble_fidelity_report(
                "refund-env",
                manifest=deepcopy(REFUND_FIDELITY_MANIFEST),
                bogus_kwarg=[])
        with self.assertRaises(ValueError):
            assemble_fidelity_report(
                "other-env",
                manifest=deepcopy(REFUND_FIDELITY_MANIFEST))


if __name__ == "__main__":
    unittest.main()
