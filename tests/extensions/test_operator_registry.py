"""R1 operator registry: register, import, gate. Temp databases only."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ghostlab.adapters.reservation_env import ReservationEnvironment
from ghostlab.extensions.manifest import (canonical_digest, parse_manifest_bytes,
                                          validate_manifest)
from ghostlab.extensions.operator_registry import (
    STATUS_NOT_QUALIFIED, STATUS_QUALIFIED, OperatorRegistry,
    adapter_code_digest, check_adapter_ref_allowed, resolve_adapter_ref)
from ghostlab.extensions.registry import AdapterRegistry

RESERVATION_REF = "ghostlab.adapters.reservation_env:ReservationEnvironment"
RESERVATION_WORLD = "reservation-world/1"


class BrokenResetAdapter(ReservationEnvironment):
    """Ignores the requested fixture; always resets to half_full."""

    def reset(self, fixture, scenario, *, actor_id):
        fixed = dict(fixture)
        fixed["fixture"] = "half_full"
        return super().reset(fixed, scenario, actor_id=actor_id)


class SharedServiceAdapter(ReservationEnvironment):
    """Shares one service and trace buffer per fixture (breaks isolation)."""

    _services = {}
    _traces = {}

    def reset(self, fixture, scenario, *, actor_id):
        super().reset(fixture, scenario, actor_id=actor_id)
        name = dict(fixture).get("fixture", "half_full")
        if name not in SharedServiceAdapter._services:
            SharedServiceAdapter._services[name] = self._service
            SharedServiceAdapter._traces[name] = self._trace
        self._service = SharedServiceAdapter._services[name]
        self._trace = SharedServiceAdapter._traces[name]
        return self.observe()


class IncompleteAdapter:
    """Missing most of the environment protocol."""

    def describe(self):
        raise NotImplementedError


def reservation_manifest(adapter_ref=None):
    return {
        "manifest_version": 1, "world_version": RESERVATION_WORLD,
        "environment_id": "reservation-environment",
        "environment_version": "reservation-environment/1",
        "adapter_ref": adapter_ref or RESERVATION_REF,
        "display_name": "Reservation demo",
        "tools": [{"name": name, "parameters": []} for name in
                  ("check_availability", "reserve", "cancel", "get_status")],
        "fixtures": ["half_full", "near_full"],
        "fault_hooks": ["timeout_after_commit", "stale_snapshot",
                        "skip_capacity_check"],
        "invariants": ["capacity_respected", "records_wellformed",
                       "used_consistent", "used_nonnegative"],
        "capabilities": {"snapshot_restore": False},
        "reproduction_modes": ["recorded-actions"],
        "notes": "test manifest",
    }


def fresh_registry(test_case):
    directory = tempfile.TemporaryDirectory(prefix="ghostlab-opreg-test-")
    test_case.addCleanup(directory.cleanup)
    return OperatorRegistry(
        Path(directory.name) / "registry.sqlite",
        allowed_adapter_prefixes=("ghostlab.", __name__))


def register_reservation(operator, ref=RESERVATION_REF, world=RESERVATION_WORLD):
    return operator.register(
        world_version=world, environment_id="reservation-environment",
        environment_version="reservation-environment/1", adapter_ref=ref,
        display_name="Reservation demo",
        declared_tools=[{"name": "reserve", "parameters": ["holder", "seats"]}],
        declared_modes=["recorded-actions"], registered_by="operator-test")


class ManifestTest(unittest.TestCase):
    def test_valid_manifest_roundtrip(self):
        manifest = reservation_manifest()
        validated = validate_manifest(json.loads(json.dumps(manifest)))
        self.assertEqual(validated["world_version"], RESERVATION_WORLD)
        digest = canonical_digest(validated)
        self.assertEqual(digest, canonical_digest(dict(validated)))
        self.assertEqual(len(digest), 64)

    def test_unknown_fields_rejected(self):
        manifest = reservation_manifest()
        manifest["code"] = "evil()"
        with self.assertRaises(ValueError):
            validate_manifest(manifest)

    def test_urls_paths_nul_rejected(self):
        for poison in ("https://example.com/x", "/abs/path", "C:\\temp\\x",
                       "\\\\host\\share", "a\x00b"):
            manifest = reservation_manifest()
            manifest["notes"] = poison
            with self.assertRaises(ValueError, msg=poison):
                validate_manifest(manifest)

    def test_bytes_bound_and_dupe_keys(self):
        with self.assertRaises(ValueError):
            parse_manifest_bytes(b"x" * 300_000)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_manifest_bytes(b'{"a": 1, "a": 2}')


class LoaderTest(unittest.TestCase):
    def test_allowlist_enforced(self):
        with self.assertRaisesRegex(ValueError, "allowlist"):
            check_adapter_ref_allowed("os:System", ("ghostlab.",))
        check_adapter_ref_allowed(RESERVATION_REF, ("ghostlab.",))

    def test_missing_methods_rejected(self):
        with self.assertRaisesRegex(ValueError, "misses"):
            resolve_adapter_ref(f"{__name__}:IncompleteAdapter", (__name__,))

    def test_resolve_and_digest(self):
        cls = resolve_adapter_ref(RESERVATION_REF, ("ghostlab.",))
        self.assertIs(cls, ReservationEnvironment)
        digest = adapter_code_digest(cls)
        self.assertEqual(len(digest), 64)


class RegistryTest(unittest.TestCase):
    def test_register_get_list(self):
        operator = fresh_registry(self)
        record = register_reservation(operator)
        self.assertEqual(record["status"], STATUS_NOT_QUALIFIED)
        self.assertEqual(operator.get(RESERVATION_WORLD)["adapter_ref"],
                         RESERVATION_REF)
        envs = operator.list_envs()
        self.assertEqual([env["world_version"] for env in envs], [RESERVATION_WORLD])
        with self.assertRaisesRegex(ValueError, "already registered"):
            register_reservation(operator)

    def test_unknown_world_refused(self):
        operator = fresh_registry(self)
        with self.assertRaises(LookupError):
            operator.get("nope/1")

    def test_import_manifest_pins_digest(self):
        operator = fresh_registry(self)
        register_reservation(operator)
        data = json.dumps(reservation_manifest()).encode()
        result = operator.import_manifest(data)
        self.assertEqual(result["manifest_digest"],
                         canonical_digest(reservation_manifest()))
        fetched = operator.get_manifest(result["manifest_digest"])
        self.assertEqual(fetched["world_version"], RESERVATION_WORLD)

    def test_import_rejects_mismatch_and_unknown(self):
        operator = fresh_registry(self)
        register_reservation(operator)
        other = reservation_manifest("ghostlab.adapters.refund_env:RefundEnvironment")
        with self.assertRaisesRegex(ValueError, "does not match"):
            operator.import_manifest(json.dumps(other).encode())
        missing = reservation_manifest()
        missing["world_version"] = "missing-world/1"
        with self.assertRaises(LookupError):
            operator.import_manifest(json.dumps(missing).encode())

    def test_gate_refuses_until_qualified(self):
        operator = fresh_registry(self)
        register_reservation(operator)
        with self.assertRaisesRegex(ValueError, "not qualified"):
            operator.load_into(AdapterRegistry(), RESERVATION_WORLD)

    def test_gate_refuses_code_change(self):
        operator = fresh_registry(self)
        register_reservation(operator)
        operator.record_qualification(world_version=RESERVATION_WORLD,
                                      checks=[{"name": "x", "passed": True,
                                               "details": "y"}],
                                      passed=True)
        connection = sqlite3.connect(str(operator._path))
        try:
            connection.execute("UPDATE registrations SET code_digest = '0' * 64"
                               " WHERE world_version = ?", (RESERVATION_WORLD,))
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(ValueError, "changed since review"):
            operator.load_into(AdapterRegistry(), RESERVATION_WORLD)

    def test_gate_loads_after_qualification(self):
        operator = fresh_registry(self)
        register_reservation(operator)
        stored = operator.record_qualification(
            world_version=RESERVATION_WORLD,
            checks=[{"name": "x", "passed": True, "details": "y"}], passed=True)
        self.assertEqual(stored["status"], STATUS_QUALIFIED)
        factory = operator.load_into(AdapterRegistry(), RESERVATION_WORLD)
        env = factory()
        try:
            self.assertEqual(env.describe().world_version, RESERVATION_WORLD)
        finally:
            env.close()


class SabotageTest(unittest.TestCase):
    def _qualify_ref(self, ref):
        import io
        from contextlib import redirect_stdout
        from ghostlab.extensions.operator_cli import main
        directory = tempfile.TemporaryDirectory(prefix="ghostlab-opreg-test-")
        self.addCleanup(directory.cleanup)
        db = str(Path(directory.name) / "registry.sqlite")
        base = ["--db", db, "--allow-prefix", "ghostlab.", "--allow-prefix", __name__]
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["register"] + base + [
                "--world", RESERVATION_WORLD, "--env-id", "reservation-environment",
                "--env-version", "reservation-environment/1", "--adapter-ref", ref,
                "--display-name", "Sabotaged",
                "--tools", json.dumps([{"name": "reserve",
                                        "parameters": ["holder", "seats"]}]),
                "--modes", json.dumps(["recorded-actions"]),
                "--registered-by", "operator-test"])
        self.assertEqual(code, 0, out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["env-qualify"] + base + [
                "--world", RESERVATION_WORLD, "--fixture", "near_full",
                "--scenario", json.dumps({"timeout_after_commit": False,
                                          "stale_snapshot": False,
                                          "skip_capacity_check": False,
                                          "max_tool_calls": 20}),
                "--script", json.dumps([
                    {"tool": "check_availability", "arguments": {}},
                    {"tool": "reserve", "arguments": {"holder": "demo", "seats": 1}}]),
                "--task-id", "probe", "--actor", "probe"])
        self.assertEqual(code, 0, out.getvalue())
        return json.loads(out.getvalue())

    def test_broken_reset_fails_qualification(self):
        result = self._qualify_ref(f"{__name__}:BrokenResetAdapter")
        self.assertFalse(result["report"]["passed"])
        self.assertEqual(result["stored"]["status"], "qualification failed")
        checks = {check["name"]: check["passed"]
                  for check in result["report"]["checks"]}
        self.assertIn("reset_correctness", checks)
        self.assertFalse(checks["reset_correctness"])

    def test_broken_isolation_fails_qualification(self):
        SharedServiceAdapter._services.clear()
        SharedServiceAdapter._traces.clear()
        self.addCleanup(SharedServiceAdapter._services.clear)
        self.addCleanup(SharedServiceAdapter._traces.clear)
        result = self._qualify_ref(f"{__name__}:SharedServiceAdapter")
        self.assertFalse(result["report"]["passed"])
        checks = {check["name"]: check["passed"]
                  for check in result["report"]["checks"]}
        self.assertIn("run_isolation", checks)
        self.assertFalse(checks["run_isolation"])


class CliTest(unittest.TestCase):
    def _run(self, argv, db):
        import io
        from contextlib import redirect_stdout
        from ghostlab.extensions.operator_cli import main
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(argv + ["--db", db, "--allow-prefix", "ghostlab."])
        return code, out.getvalue()

    def _registered_db(self):
        directory = tempfile.TemporaryDirectory(prefix="ghostlab-opreg-test-")
        self.addCleanup(directory.cleanup)
        db = str(Path(directory.name) / "registry.sqlite")
        code, out = self._run(
            ["register", "--world", RESERVATION_WORLD, "--env-id",
             "reservation-environment", "--env-version", "reservation-environment/1",
             "--adapter-ref", RESERVATION_REF, "--display-name", "Reservation demo",
             "--tools", json.dumps([{"name": "reserve",
                                     "parameters": ["holder", "seats"]}]),
             "--modes", json.dumps(["recorded-actions"]),
             "--registered-by", "operator-test"], db)
        self.assertEqual(code, 0, out)
        return db

    def test_flow_to_qualified(self):
        db = self._registered_db()
        code, out = self._run(["list"], db)
        self.assertEqual(code, 0, out)
        self.assertIn(RESERVATION_WORLD, out)
        code, out = self._run(["capabilities", "--world", RESERVATION_WORLD], db)
        self.assertEqual(code, 0, out)
        self.assertIn("check_availability", out)
        code, out = self._run(
            ["env-qualify", "--world", RESERVATION_WORLD, "--fixture", "half_full",
             "--scenario", json.dumps({"timeout_after_commit": False,
                                       "stale_snapshot": False,
                                       "skip_capacity_check": False,
                                       "max_tool_calls": 20}),
             "--script", json.dumps([
                 {"tool": "check_availability", "arguments": {}},
                 {"tool": "reserve", "arguments": {"holder": "demo", "seats": 1}}]),
             "--task-id", "probe", "--actor", "probe"], db)
        self.assertEqual(code, 0, out)
        result = json.loads(out)
        self.assertTrue(result["report"]["passed"])
        self.assertEqual(result["stored"]["status"],
                         "qualified for declared capabilities")
        operator = OperatorRegistry(
            db, allowed_adapter_prefixes=("ghostlab.",))
        factory = operator.load_into(AdapterRegistry(), RESERVATION_WORLD)
        self.assertIs(factory, ReservationEnvironment)

    def test_manifest_agreement(self):
        db = self._registered_db()
        describe = ReservationEnvironment().describe()
        manifest = reservation_manifest()
        manifest["tools"] = [{"name": tool.name, "parameters": []}
                             for tool in describe.tools]
        manifest["fixtures"] = list(describe.fixtures)
        manifest["fault_hooks"] = [fault.fault_id for fault in describe.faults]
        manifest["invariants"] = list(describe.invariants)
        manifest["capabilities"] = dict(describe.capabilities)
        path = Path(db).parent / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        code, out = self._run(["import-manifest", "--manifest", str(path)], db)
        self.assertEqual(code, 0, out)
        digest = json.loads(out)["manifest_digest"]
        qualify = ["env-qualify", "--world", RESERVATION_WORLD, "--fixture",
                   "half_full", "--scenario", json.dumps(
                       {"timeout_after_commit": False, "stale_snapshot": False,
                        "skip_capacity_check": False, "max_tool_calls": 20}),
                   "--script", json.dumps([{"tool": "check_availability",
                                            "arguments": {}}]),
                   "--task-id", "probe", "--actor", "probe",
                   "--manifest-digest", digest]
        code, out = self._run(qualify, db)
        self.assertEqual(code, 0, out)
        checks = {check["name"]: check["passed"]
                  for check in json.loads(out)["report"]["checks"]}
        self.assertTrue(checks["manifest_agreement"])
        manifest["capabilities"] = {"snapshot_restore": True,
                                    "forged": True}
        path.write_text(json.dumps(manifest), encoding="utf-8")
        code, out = self._run(["import-manifest", "--manifest", str(path)], db)
        self.assertEqual(code, 0, out)
        bad_digest = json.loads(out)["manifest_digest"]
        code, out = self._run(qualify[:-1] + [bad_digest], db)
        self.assertEqual(code, 0, out)
        result = json.loads(out)
        checks = {check["name"]: check["passed"]
                  for check in result["report"]["checks"]}
        self.assertFalse(checks["manifest_agreement"])
        self.assertEqual(result["stored"]["status"], "qualification failed")

    def test_unknown_and_invalid_refused(self):
        db = self._registered_db()
        code, out = self._run(["capabilities", "--world", "nope/1"], db)
        self.assertEqual(code, 1)
        code, out = self._run(
            ["env-qualify", "--world", RESERVATION_WORLD, "--fixture", "half_full",
             "--scenario", "not-json", "--script", "[]", "--task-id", "probe",
             "--actor", "probe"], db)
        self.assertEqual(code, 1)

if __name__ == "__main__":
    unittest.main()
