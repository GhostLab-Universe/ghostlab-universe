"""Requested/resolved configs, store and diffs over P1 worlds."""

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ghostlab.experiments.configs import (
    ConfigError, canonical_digest, check_project_id, requested_config_id,
    resolve_requested, scan_for_secrets, validate_policy, validate_requested,
    validate_resolved)
from ghostlab.experiments.diffs import diff_resolved
from ghostlab.experiments.store import CrossProjectError, ExperimentStore
from ghostlab.extensions.operator_registry import OperatorRegistry
from ghostlab.extensions.registry import (
    AdapterManifest, AdapterRegistry)

WORLD = "reservation-world/1"
REF = "ghostlab.adapters.reservation_env:ReservationEnvironment"


def fresh_operators(test_case):
    directory = tempfile.TemporaryDirectory(prefix="ghostlab-exp-test-")
    test_case.addCleanup(directory.cleanup)
    operator = OperatorRegistry(
        Path(directory.name) / "registry.sqlite",
        allowed_adapter_prefixes=("ghostlab.",))
    operator.register(
        world_version=WORLD, environment_id="reservation-environment",
        environment_version="reservation-environment/1", adapter_ref=REF,
        display_name="Reservation demo",
        declared_tools=[{"name": "reserve", "parameters": ["holder", "seats"]}],
        declared_modes=["recorded-actions"], registered_by="operator-test")
    return operator


def fresh_targets():
    targets = AdapterRegistry()
    targets.register_target(
        WORLD, "reservation-target", lambda: object(),
        AdapterManifest(package="test-targets", version="reservation-target/1",
                        origin="checked_in"))
    return targets


def fresh_store(test_case):
    directory = tempfile.TemporaryDirectory(prefix="ghostlab-exp-store-")
    test_case.addCleanup(directory.cleanup)
    return ExperimentStore(Path(directory.name) / "experiments.sqlite")


def policy(**overrides):
    body = {"policy_version": "policy/1", "ceiling_tool_calls": 50,
            "ceiling_total_tool_calls": 200, "ceiling_repetitions": 5,
            "ceiling_wall_clock_ms": 60000,
            "allowed_targets": ["reservation-target", "script"],
            "allowed_hooks": ["timeout_after_commit", "stale_snapshot",
                              "skip_capacity_check"]}
    body.update(overrides)
    return body


def requested(**overrides):
    body = {"config_version": 1, "name": "dev-config", "world_version": WORLD,
            "target": {"kind": "script",
                       "actions": [{"tool": "reserve",
                                    "arguments": {"holder": "amy", "seats": 2}}]},
            "fixture": {"name": "half_full"},
            "faults": [{"hook": "timeout_after_commit", "params": {},
                        "required": True}],
            "rules": ["capacity_respected"],
            "limits": {"max_tool_calls": 10, "max_total_tool_calls": 20,
                       "max_wall_clock_ms": 5000},
            "repetitions": {"count": 2}}
    body.update(overrides)
    return body


class PolicyTest(unittest.TestCase):
    def test_valid_policy(self):
        self.assertEqual(validate_policy(policy())["policy_version"], "policy/1")

    def test_policy_keys_exact(self):
        bad = policy()
        del bad["allowed_hooks"]
        with self.assertRaises(ConfigError):
            validate_policy(bad)
        bad = policy(extra=1)
        with self.assertRaises(ConfigError):
            validate_policy(bad)

    def test_policy_ceiling_bounds(self):
        with self.assertRaises(ConfigError):
            validate_policy(policy(ceiling_tool_calls=0))
        with self.assertRaises(ConfigError):
            validate_policy(policy(ceiling_repetitions=26))

    def test_policy_lists_strict(self):
        with self.assertRaises(ConfigError):
            validate_policy(policy(allowed_hooks=["Bogus-Hook"]))
        with self.assertRaises(ConfigError):
            validate_policy(policy(allowed_targets=["a", "a"]))


class ValidateRequestedTest(unittest.TestCase):
    def test_valid_requested(self):
        clean = validate_requested(requested(), policy())
        self.assertEqual(clean["limits"]["max_tool_calls"], 10)
        self.assertEqual(clean["repetitions"]["count"], 2)

    def test_unknown_fields_rejected(self):
        bad = requested(extra=True)
        with self.assertRaises(ConfigError):
            validate_requested(bad, policy())
        bad = requested()
        bad["limits"]["extra"] = 1
        with self.assertRaises(ConfigError):
            validate_requested(bad, policy())
        bad = requested()
        bad["target"]["actions"][0]["extra"] = 1
        with self.assertRaises(ConfigError):
            validate_requested(bad, policy())

    def test_strict_types(self):
        bad = requested()
        bad["limits"]["max_tool_calls"] = "10"
        with self.assertRaises(ConfigError):
            validate_requested(bad, policy())
        bad = requested()
        bad["faults"][0]["required"] = "yes"
        with self.assertRaises(ConfigError):
            validate_requested(bad, policy())
        with self.assertRaises(ConfigError):
            validate_requested(requested(config_version="1"), policy())

    def test_ceilings_rejected_never_clamped(self):
        with self.assertRaises(ConfigError):
            validate_requested(requested(limits={
                "max_tool_calls": 51, "max_total_tool_calls": 200,
                "max_wall_clock_ms": 5000}), policy())
        with self.assertRaises(ConfigError):
            validate_requested(requested(repetitions={"count": 6}), policy())

    def test_impossible_aggregate_rejected(self):
        bad = requested(limits={"max_tool_calls": 10,
                                "max_total_tool_calls": 19,
                                "max_wall_clock_ms": 5000})
        with self.assertRaises(ConfigError):
            validate_requested(bad, policy())

    def test_policy_allow_lists(self):
        target = {"kind": "registered", "id": "other-target"}
        with self.assertRaises(ConfigError):
            validate_requested(requested(target=target), policy())
        with self.assertRaises(ConfigError):
            validate_requested(requested(), policy(allowed_targets=[]))
        bad = requested()
        bad["faults"][0]["hook"] = "stale_snapshot"
        with self.assertRaises(ConfigError):
            validate_requested(bad, policy(allowed_hooks=["timeout_after_commit"]))

    def test_duplicates_rejected(self):
        bad = requested()
        bad["faults"] = [dict(bad["faults"][0]), dict(bad["faults"][0])]
        with self.assertRaises(ConfigError):
            validate_requested(bad, policy())
        with self.assertRaises(ConfigError):
            validate_requested(requested(rules=["capacity_respected"] * 2),
                               policy())

    def test_script_bounds(self):
        with self.assertRaises(ConfigError):
            validate_requested(
                requested(target={"kind": "script", "actions": []}), policy())
        many = [{"tool": "reserve", "arguments": {}} for _ in range(101)]
        with self.assertRaises(ConfigError):
            validate_requested(
                requested(target={"kind": "script", "actions": many}), policy())
        with self.assertRaises(ConfigError):
            validate_requested(requested(target={"kind": "shell"}), policy())

    def test_secret_markers_rejected_by_scan(self):
        with self.assertRaises(ConfigError):
            scan_for_secrets({"name": "x API_KEY=abc"})
        scan_for_secrets(requested())

    def test_requested_id_stable_and_project_scoped(self):
        body = validate_requested(requested(), policy())
        first = requested_config_id("proj-a", body)
        self.assertEqual(first, requested_config_id("proj-a", copy.deepcopy(body)))
        self.assertNotEqual(first, requested_config_id("proj-b", body))
        self.assertTrue(first.startswith("req-"))

    def test_project_ids_checked(self):
        self.assertEqual(check_project_id("proj-a"), "proj-a")
        with self.assertRaises(ConfigError):
            check_project_id("../escape")


class DescribeOnlyTest(unittest.TestCase):
    def test_describe_only_shape(self):
        operator = fresh_operators(self)
        described = operator.describe_only(WORLD)
        self.assertEqual(described["world_version"], WORLD)
        self.assertIn("reserve", [tool["name"] for tool in described["tools"]])
        self.assertEqual(
            {fault["id"] for fault in described["faults"]},
            {"timeout_after_commit", "stale_snapshot", "skip_capacity_check"})
        self.assertIn("half_full", described["fixtures"])
        self.assertIn("capacity_respected", described["invariants"])
        self.assertEqual(described["modes"], ["recorded-actions"])
        json.dumps(described)

    def test_describe_only_unknown_world(self):
        with self.assertRaises(LookupError):
            fresh_operators(self).describe_only("missing-world/9")


class ResolveTest(unittest.TestCase):
    def resolve(self, body=None, operator=None, **kwargs):
        operator = operator or fresh_operators(self)
        clean = validate_requested(body or requested(), policy())
        params = {"project_id": "proj-a", "requested_id": "req-0123456789abcdef",
                  "requested_digest": canonical_digest(clean),
                  "requested_by": "dev", "policy": policy(),
                  "operators": operator, "targets": fresh_targets(),
                  "now_iso": "2026-11-02T00:00:00+00:00"}
        params.update(kwargs)
        return resolve_requested(clean, **params)

    def test_script_target_resolves(self):
        resolved = self.resolve()
        pins = resolved["pins"]
        self.assertEqual(pins["world_version"], WORLD)
        self.assertEqual(pins["environment_id"], "reservation-environment")
        self.assertIn("ghostlab.adapters.reservation_env", pins["adapter_ref"])
        self.assertEqual(len(pins["code_digest"]), 64)
        self.assertIsNone(pins["manifest_digest"])
        self.assertEqual(pins["target_id"], "inline-script")
        self.assertEqual(pins["target_version"], "inline-script/1")
        self.assertEqual(len(pins["script_digest"]), 64)
        self.assertIn("reserve", pins["discovered_tools"])
        self.assertEqual(pins["discovered_modes"], ["recorded-actions"])
        self.assertIn("half_full", pins["discovered_fixtures"])
        effective = resolved["effective"]
        self.assertEqual(effective["scenario"],
                         {"timeout_after_commit": True, "max_tool_calls": 10})
        self.assertEqual(resolved["provenance"]["qualification_status"],
                         "not qualified")
        self.assertTrue(resolved["resolved_config_id"].startswith("res-"))
        validate_resolved(resolved)

    def test_registered_target_resolves(self):
        body = requested(target={"kind": "registered", "id": "reservation-target"})
        resolved = self.resolve(body)
        self.assertEqual(resolved["pins"]["target_id"], "reservation-target")
        self.assertEqual(resolved["pins"]["target_version"], "reservation-target/1")
        self.assertIsNone(resolved["pins"]["script_digest"])

    def test_unsupported_settings_rejected(self):
        operator = fresh_operators(self)
        bad = requested(fixture={"name": "penthouse"})
        with self.assertRaises(ConfigError):
            self.resolve(bad, operator=operator)
        with self.assertRaises(ConfigError):
            self.resolve(requested(rules=["nope_missing"]), operator=operator)
        bad = requested()
        bad["faults"][0]["hook"] = "nope_hook"
        with self.assertRaises(ConfigError):
            self.resolve(bad, operator=operator)
        bad = requested()
        bad["target"]["actions"][0]["tool"] = "nope_tool"
        with self.assertRaises(ConfigError):
            self.resolve(bad, operator=operator)
        bad = requested(target={"kind": "registered", "id": "reservation-target"})
        with self.assertRaises(ConfigError):
            self.resolve(bad, operator=operator, targets=AdapterRegistry())

    def test_undeclared_fault_params_rejected(self):
        bad = requested()
        bad["faults"][0]["params"] = {"delay_ms": 5}
        with self.assertRaises(ConfigError):
            self.resolve(bad)

    def test_unregistered_world_rejected(self):
        bad = requested(world_version="refund-world/1")
        with self.assertRaises(ConfigError):
            self.resolve(bad)

    def test_manifest_pin_and_mismatch(self):
        operator = fresh_operators(self)
        manifest = {"manifest_version": 1, "world_version": WORLD,
                    "environment_id": "reservation-environment",
                    "environment_version": "reservation-environment/1",
                    "adapter_ref": REF, "display_name": "Reservation demo",
                    "tools": [{"name": name, "parameters": []} for name in
                              ("check_availability", "reserve", "cancel",
                               "get_status")],
                    "fixtures": ["empty_small", "full", "half_full",
                                "near_full"],
                    "fault_hooks": ["timeout_after_commit", "stale_snapshot",
                                    "skip_capacity_check"],
                    "invariants": ["capacity_respected", "records_wellformed",
                                   "used_consistent", "used_nonnegative"],
                    "capabilities": {"snapshot_restore": False},
                    "reproduction_modes": ["recorded-actions"],
                    "notes": "test manifest"}
        imported = operator.import_manifest(
            json.dumps(manifest).encode())
        resolved = self.resolve(operator=operator,
                                manifest_digest=imported["manifest_digest"])
        self.assertEqual(resolved["pins"]["manifest_digest"],
                         imported["manifest_digest"])
        with self.assertRaises(ConfigError):
            self.resolve(operator=operator, manifest_digest="0" * 64)
        manifest["tools"] = [{"name": "bogus_tool", "parameters": []}]
        forged = operator.import_manifest(json.dumps(manifest).encode())
        with self.assertRaises(ConfigError):
            self.resolve(operator=operator,
                             manifest_digest=forged["manifest_digest"])

    def test_resolve_idempotent_across_metadata(self):
        first = self.resolve(operator=fresh_operators(self))
        second = self.resolve(operator=fresh_operators(self),
                              resolved_by="someone-else",
                              now_iso="2026-11-03T00:00:00+00:00")
        self.assertEqual(first["resolved_config_id"], second["resolved_config_id"])
        self.assertNotEqual(first["resolved_at"], second["resolved_at"])

    def test_qualification_recorded_and_repinned(self):
        operator = fresh_operators(self)
        before = self.resolve(operator=operator)
        operator.record_qualification(
            world_version=WORLD, checks=[{"name": "reset", "passed": True}],
            passed=True)
        after = self.resolve(operator=operator)
        self.assertEqual(after["provenance"]["qualification_status"], "qualified for declared capabilities")
        self.assertNotEqual(before["resolved_config_id"],
                            after["resolved_config_id"])

    def test_validate_resolved_rejects_tampering(self):
        resolved = self.resolve()
        tampered = copy.deepcopy(resolved)
        tampered["pins"]["world_version"] = "refund-world/1"
        with self.assertRaises(ConfigError):
            validate_resolved(tampered)
        tampered = copy.deepcopy(resolved)
        del tampered["provenance"]
        with self.assertRaises(ConfigError):
            validate_resolved(tampered)


class StoreTest(unittest.TestCase):
    def test_requested_roundtrip_idempotent(self):
        store = fresh_store(self)
        body = validate_requested(requested(), policy())
        first = store.save_requested(
            project_id="proj-a", body=body, digest=canonical_digest(body),
            requested_by="dev", created_at="2026-11-02T00:00:00+00:00")
        second = store.save_requested(
            project_id="proj-a", body=body, digest=canonical_digest(body),
            requested_by="dev", created_at="2026-11-02T00:00:01+00:00")
        self.assertEqual(first["config_id"], second["config_id"])
        fetched = store.get_requested("proj-a", first["config_id"])
        self.assertEqual(fetched["config"], body)
        self.assertEqual(len(store.list_requested("proj-a")), 1)
        self.assertEqual(store.list_requested("proj-b"), [])

    def test_cross_project_refused(self):
        store = fresh_store(self)
        body = validate_requested(requested(), policy())
        saved = store.save_requested(
            project_id="proj-a", body=body, digest=canonical_digest(body),
            requested_by="dev", created_at="2026-11-02T00:00:00+00:00")
        with self.assertRaises(CrossProjectError):
            store.get_requested("proj-b", saved["config_id"])

    def test_corruption_fails_cleanly(self):
        store = fresh_store(self)
        body = validate_requested(requested(), policy())
        saved = store.save_requested(
            project_id="proj-a", body=body, digest=canonical_digest(body),
            requested_by="dev", created_at="2026-11-02T00:00:00+00:00")
        connection = sqlite3.connect(store.db_path)
        try:
            connection.execute(
                "UPDATE requested_configs SET config_json = ? WHERE config_id = ?",
                (json.dumps({"config_version": 1}), saved["config_id"]))
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(ValueError):
            store.get_requested("proj-a", saved["config_id"])

    def test_secrets_rejected_at_save(self):
        store = fresh_store(self)
        body = validate_requested(requested(), policy())
        body["name"] = "holds BEARER token-x"
        with self.assertRaises(ConfigError):
            store.save_requested(
                project_id="proj-a", body=body, digest=canonical_digest(body),
                requested_by="dev", created_at="2026-11-02T00:00:00+00:00")

    def test_resolved_roundtrip(self):
        store = fresh_store(self)
        operator = fresh_operators(self)
        clean = validate_requested(requested(), policy())
        resolved = resolve_requested(
            clean, project_id="proj-a", requested_id="req-0123456789abcdef",
            requested_digest=canonical_digest(clean), requested_by="dev",
            policy=policy(), operators=operator, targets=fresh_targets(),
            now_iso="2026-11-02T00:00:00+00:00")
        saved = store.save_resolved(
            project_id="proj-a", requested_id="req-0123456789abcdef",
            body=resolved, digest=resolved["digest"],
            created_at="2026-11-02T00:00:00+00:00")
        self.assertEqual(saved["resolved"], resolved)
        self.assertEqual(len(store.list_resolved("proj-a")), 1)

    def test_runs_single_attempt_and_ledger(self):
        store = fresh_store(self)
        run = {"status": "completed", "termination_reason": "script-exhausted",
               "tool_calls": 3, "failed_rules": [],
               "checks": [{"name": "capacity_respected", "passed": True}]}
        saved = store.save_run(
            project_id="proj-a", resolved_id="res-abc", repetition=1,
            attempt=1, body=run, created_at="2026-11-02T00:00:00+00:00")
        self.assertTrue(saved["run_id"].startswith("run-"))
        with self.assertRaises(ValueError):
            store.save_run(
                project_id="proj-a", resolved_id="res-abc", repetition=1,
                attempt=1, body=run, created_at="2026-11-02T00:00:01+00:00")
        self.assertTrue(store.attempt_recorded("res-abc", 1, 1))
        self.assertFalse(store.attempt_recorded("res-abc", 1, 2))
        self.assertEqual(len(store.list_runs("proj-a", "res-abc")), 1)
        store.add_ledger(project_id="proj-a", resolved_id="res-abc",
                         run_id=saved["run_id"], resource="tool_calls",
                         amount=3, created_at="2026-11-02T00:00:00+00:00")
        self.assertEqual(store.budget_used("proj-a", "res-abc"), 3)
        self.assertEqual(store.budget_used("proj-a", "res-other"), 0)


    def test_atomic_run_with_ledger(self):
        store = fresh_store(self)
        run = {"status": "completed", "termination_reason": "target-finished",
               "tool_calls": 3, "failed_rules": [],
               "checks": {"capacity_respected": True}}
        saved = store.save_run_with_ledger(
            project_id="proj-a", resolved_id="res-atomic", repetition=1,
            attempt=1, body=run, tool_calls=3, wall_clock_ms=41,
            created_at="2026-11-02T00:00:00+00:00")
        self.assertTrue(saved["run_id"].startswith("run-"))
        self.assertEqual(store.budget_used("proj-a", "res-atomic"), 3)
        self.assertEqual(
            store.budget_used("proj-a", "res-atomic", "wall_clock_ms"), 41)
        with self.assertRaises(ValueError):
            store.save_run_with_ledger(
                project_id="proj-a", resolved_id="res-atomic", repetition=1,
                attempt=1, body=run, tool_calls=3, wall_clock_ms=41,
                created_at="2026-11-02T00:00:01+00:00")

class DiffTest(unittest.TestCase):
    def test_equal_resolutions(self):
        operator = fresh_operators(self)
        clean = validate_requested(requested(), policy())
        kwargs = {"project_id": "proj-a",
                  "requested_id": "req-0123456789abcdef",
                  "requested_digest": canonical_digest(clean),
                  "requested_by": "dev", "policy": policy(),
                  "operators": operator, "targets": fresh_targets()}
        left = resolve_requested(clean, now_iso="2026-11-02T00:00:00+00:00",
                                 **kwargs)
        right = resolve_requested(clean, now_iso="2026-11-02T00:00:01+00:00",
                                  **kwargs)
        compared = diff_resolved(left, right)
        self.assertTrue(compared["equal"])
        self.assertEqual(compared["diff_count"], 0)

    def test_changed_setting_reported(self):
        operator = fresh_operators(self)
        left = resolve_requested(
            validate_requested(requested(), policy()), project_id="proj-a",
            requested_id="req-0123456789abcdef",
            requested_digest=canonical_digest(
                validate_requested(requested(), policy())),
            requested_by="dev", policy=policy(), operators=operator,
            targets=fresh_targets(), now_iso="2026-11-02T00:00:00+00:00")
        other = requested()
        other["faults"] = []
        clean = validate_requested(other, policy())
        right = resolve_requested(
            clean, project_id="proj-a", requested_id="req-0123456789abcdef",
            requested_digest=canonical_digest(clean), requested_by="dev",
            policy=policy(), operators=operator, targets=fresh_targets(),
            now_iso="2026-11-02T00:00:00+00:00")
        compared = diff_resolved(left, right)
        self.assertFalse(compared["equal"])
        paths = [entry["path"] for entry in compared["diffs"]]
        self.assertIn("effective.scenario.timeout_after_commit", paths)
        entry = next(item for item in compared["diffs"]
                     if item["path"] == "effective.scenario.timeout_after_commit")
        self.assertEqual((entry["left"], entry["right"]), (True, None))


if __name__ == "__main__":
    unittest.main()