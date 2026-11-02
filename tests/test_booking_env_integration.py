"""Integration: external booking example through the shared env pipeline.

Proves an external environment (``examples/equipment_booking``, outside
``ghostlab`` core) registers, imports a manifest, discloses capabilities and
qualifies through Track A's pipeline with zero edits to core code, and that
the experiment controller refuses to execute unqualified, failed, or
code-changed environments.

Every test uses temporary databases; no live registry file is touched.
"""

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.equipment_booking.env_adapter import (  # noqa: E402
    BOOKING_TOOLS, BookingEnvAdapter)
from ghostlab.envs.adapters import RefundEnvAdapter  # noqa: E402
from ghostlab.envs.capabilities import discover  # noqa: E402
from ghostlab.envs.cli import main as env_main  # noqa: E402
from ghostlab.envs.manifest import import_manifest  # noqa: E402
from ghostlab.envs.qualification import run_qualification  # noqa: E402
from ghostlab.envs.registry import EnvRegistry  # noqa: E402
from ghostlab.experiments import ops  # noqa: E402
from ghostlab.experiments.configs import ConfigError  # noqa: E402
from ghostlab.experiments.store import ExperimentStore  # noqa: E402

BOOKING_REF = ("examples.equipment_booking.env_adapter:BookingEnvAdapter")
BOOKING_MODES = ["recorded-actions", "deterministic-reset"]
BOOKING_FIXTURE = {
    "equipment_id": "synthetic-microscope-1",
    "user_id": "synthetic-user-1",
    "slot": "2026-11-03T09:00Z",
}
REFUND_REF = "ghostlab.envs.adapters:RefundEnvAdapter"


def booking_manifest() -> dict:
    return {
        "manifest_version": 1,
        "env_id": "booking-env",
        "env_version": "booking-env/1",
        "adapter_ref": BOOKING_REF,
        "display_name": "Synthetic equipment booking",
        "tools": deepcopy(BOOKING_TOOLS),
        "reproduction_modes": list(BOOKING_MODES),
        "fault_hooks": ["timeout_after_commit"],
        "notes": "External example: reservation commits then the response"
                 " times out; naive retry duplicates, stable key does not.",
    }


def make_booking_registry(directory):
    return EnvRegistry(
        Path(directory) / "env.sqlite",
        allowed_adapter_prefixes=("ghostlab.", "examples.equipment_booking.",
                                  __name__))


def register_booking(registry, env_id="booking-env"):
    return registry.register(
        env_id=env_id, env_version="booking-env/1",
        adapter_ref=BOOKING_REF, display_name="Synthetic equipment booking",
        declared_tools=deepcopy(BOOKING_TOOLS),
        declared_modes=list(BOOKING_MODES))


def refund_tools():
    return [
        {"name": "get_order", "parameters": ["order_id", "customer_id"]},
        {"name": "get_refund_status",
         "parameters": ["order_id", "customer_id", "idempotency_key"]},
        {"name": "issue_refund",
         "parameters": ["order_id", "customer_id", "amount_minor",
                        "idempotency_key"]},
    ]


def valid_policy():
    return {"policy_version": "policy/1", "ceiling_tool_calls": 20,
            "ceiling_total_tool_calls": 200, "ceiling_repetitions": 5,
            "ceiling_wall_clock_ms": 60000,
            "allowed_agents": ["vulnerable", "repaired"],
            "allowed_hooks": ["timeout_after_commit"]}


def valid_requested(env_id="refund-env"):
    return {"config_version": 1, "name": "refund timeout probe",
            "env_id": env_id, "agent": "vulnerable",
            "fixture": {"order_id": "synthetic-order-1",
                        "customer_id": "synthetic-customer-1",
                        "paid_minor": 250000, "currency": "INR"},
            "faults": [{"hook": "timeout_after_commit", "params": {},
                        "required": True}],
            "rules": ["authorized_refunds", "total_refund_within_payment",
                      "no_duplicate_full_refund", "unique_idempotency_keys"],
            "limits": {"max_tool_calls": 20, "max_total_tool_calls": 200,
                       "max_wall_clock_ms": 60000},
            "repetitions": {"count": 1}}


class BrokenBookingReset(BookingEnvAdapter):
    def reset(self, fixture):
        raise RuntimeError("boom")


class LeakyBookingAdapter(BookingEnvAdapter):
    """Shares one world across instances: reset passes, isolation fails."""

    _shared_world = None

    def reset(self, fixture):
        super().reset(fixture)
        if type(self)._shared_world is None:
            type(self)._shared_world = self._world
        else:
            self._world = type(self)._shared_world
            self._world.reset()


class BrokenRefundReset(RefundEnvAdapter):
    def reset(self, fixture):
        raise RuntimeError("boom")


class BookingPipelineTests(unittest.TestCase):
    def test_register_import_discover_qualify(self):
        with tempfile.TemporaryDirectory(
                prefix="booking-int-") as directory:
            registry = make_booking_registry(directory)
            record = register_booking(registry)
            self.assertEqual(record["status"], "not-qualified")
            imported = import_manifest(
                registry,
                json.dumps(booking_manifest()).encode("utf-8"))
            self.assertEqual(imported["env_id"], "booking-env")
            found = discover(registry, "booking-env",
                             manifest_digest=imported["manifest_digest"])
            self.assertTrue(found["match"], found["mismatches"])
            self.assertEqual(found["fault_hooks"], ["timeout_after_commit"])
            report = run_qualification(
                registry, "booking-env",
                manifest_digest=imported["manifest_digest"],
                fixture=dict(BOOKING_FIXTURE))
            self.assertEqual(report["status"], "qualified-for-declared")
            self.assertTrue(all(c["passed"] for c in report["checks"]),
                            [c for c in report["checks"] if not c["passed"]])
            self.assertEqual(report["env_version"], "booking-env/1")
            self.assertEqual(report["adapter_ref"], BOOKING_REF)
            self.assertEqual(report["manifest_digest"],
                             imported["manifest_digest"])
            self.assertEqual(report["runner_version"], "env-qualification/1")
            self.assertEqual(report["code_digest"], record["code_digest"])
            self.assertEqual(registry.get("booking-env")["status"],
                             "qualified-for-declared")

    def test_manifest_import_never_imports_adapter_code(self):
        script = (
            "import json, sys\n"
            "from ghostlab.envs.manifest import validate_manifest_bytes\n"
            "manifest = json.load(open(sys.argv[1]))\n"
            "validate_manifest_bytes(json.dumps(manifest).encode())\n"
            "mods = [m for m in sys.modules\n"
            "        if m.startswith('examples.equipment_booking')]\n"
            "print('adapter modules imported:', mods)\n"
            "sys.exit(1 if mods else 0)\n"
        )
        with tempfile.TemporaryDirectory(
                prefix="booking-int-") as directory:
            manifest_path = Path(directory) / "manifest.json"
            manifest_path.write_text(json.dumps(booking_manifest()))
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT / "src")
            completed = subprocess.run(
                [sys.executable, "-c", script, str(manifest_path)],
                capture_output=True, text=True, cwd=str(ROOT), env=env,
                timeout=60)
            self.assertEqual(completed.returncode, 0,
                             completed.stdout + completed.stderr)
            self.assertIn("adapter modules imported: []", completed.stdout)

    def test_cli_end_to_end_booking(self):
        def run_cli(argv):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = env_main(argv)
            return code, json.loads(buffer.getvalue())

        prefix = ["--allow-prefix", "examples.equipment_booking."]
        with tempfile.TemporaryDirectory(
                prefix="booking-int-") as directory:
            db = str(Path(directory) / "env.sqlite")
            manifest_path = Path(directory) / "manifest.json"
            manifest_path.write_text(json.dumps(booking_manifest()))
            code, record = run_cli(
                ["env-register", "--db", db, *prefix,
                 "--env-id", "booking-env", "--env-version", "booking-env/1",
                 "--adapter-ref", BOOKING_REF,
                 "--display-name", "Synthetic equipment booking",
                 "--tools", json.dumps(BOOKING_TOOLS),
                 "--modes", json.dumps(BOOKING_MODES)])
            self.assertEqual(code, 0)
            self.assertEqual(record["status"], "not-qualified")
            code, listed = run_cli(["env-list", "--db", db, *prefix])
            self.assertEqual(code, 0)
            self.assertEqual([e["env_id"] for e in listed["envs"]],
                             ["booking-env"])
            code, imported = run_cli(
                ["env-import-manifest", "--db", db, *prefix,
                 "--manifest", str(manifest_path)])
            self.assertEqual(code, 0)
            code, found = run_cli(
                ["env-capabilities", "--db", db, *prefix,
                 "--env-id", "booking-env",
                 "--manifest-digest", imported["manifest_digest"]])
            self.assertEqual(code, 0)
            self.assertTrue(found["match"], found["mismatches"])
            code, report = run_cli(
                ["env-qualify", "--db", db, *prefix,
                 "--env-id", "booking-env",
                 "--manifest-digest", imported["manifest_digest"],
                 "--fixture", json.dumps(BOOKING_FIXTURE)])
            self.assertEqual(code, 0)
            self.assertEqual(report["status"], "qualified-for-declared")
            code, latest = run_cli(
                ["env-report", "--db", db, *prefix,
                 "--env-id", "booking-env"])
            self.assertEqual(code, 0)
            self.assertEqual(latest["status"], "qualified-for-declared")

    def test_broken_reset_fails_qualification(self):
        LeakyBookingAdapter._shared_world = None
        with tempfile.TemporaryDirectory(
                prefix="booking-int-") as directory:
            registry = make_booking_registry(directory)
            registry.register(
                env_id="broken-env", env_version="broken/1",
                adapter_ref=f"{__name__}:BrokenBookingReset",
                display_name="Broken", declared_tools=deepcopy(BOOKING_TOOLS),
                declared_modes=list(BOOKING_MODES))
            report = run_qualification(registry, "broken-env",
                                       fixture=dict(BOOKING_FIXTURE))
            self.assertEqual(report["status"], "failed")
            self.assertTrue(any(not c["passed"] for c in report["checks"]))
            self.assertEqual(registry.get("broken-env")["status"], "failed")

    def test_broken_isolation_fails_qualification(self):
        LeakyBookingAdapter._shared_world = None
        try:
            with tempfile.TemporaryDirectory(
                    prefix="booking-int-") as directory:
                registry = make_booking_registry(directory)
                registry.register(
                    env_id="leaky-env", env_version="leaky/1",
                    adapter_ref=f"{__name__}:LeakyBookingAdapter",
                    display_name="Leaky",
                    declared_tools=deepcopy(BOOKING_TOOLS),
                    declared_modes=list(BOOKING_MODES))
                report = run_qualification(registry, "leaky-env",
                                           fixture=dict(BOOKING_FIXTURE))
                self.assertEqual(report["status"], "failed")
                by_name = {c["name"]: c for c in report["checks"]}
                self.assertTrue(by_name["reset"]["passed"])
                self.assertFalse(by_name["isolation"]["passed"])
                self.assertEqual(registry.get("leaky-env")["status"],
                                 "failed")
        finally:
            LeakyBookingAdapter._shared_world = None


class ExecutionGateTests(unittest.TestCase):
    def _resolve(self, directory, registry):
        store = ExperimentStore(Path(directory) / "experiments.sqlite")
        created = ops.create_requested_config(store, "proj-a",
                                              valid_requested(),
                                              valid_policy(),
                                              "local-operator")
        resolved = ops.resolve_config(store, registry, "proj-a",
                                      created["config_id"], valid_policy(),
                                      "local-operator")
        return store, resolved["resolved_id"]

    def test_execute_refuses_not_qualified_env(self):
        with tempfile.TemporaryDirectory(
                prefix="booking-int-") as directory:
            registry = EnvRegistry(Path(directory) / "env.sqlite")
            registry.register(
                env_id="refund-env", env_version="refund-env/1",
                adapter_ref=REFUND_REF, display_name="Synthetic refund",
                declared_tools=refund_tools(),
                declared_modes=["recorded-actions", "deterministic-reset"])
            store, resolved_id = self._resolve(directory, registry)
            with self.assertRaisesRegex(ConfigError, "not qualified"):
                ops.start_run(store, registry, "proj-a", resolved_id)

    def test_execute_refuses_failed_env(self):
        with tempfile.TemporaryDirectory(
                prefix="booking-int-") as directory:
            registry = EnvRegistry(
                Path(directory) / "env.sqlite",
                allowed_adapter_prefixes=("ghostlab.", __name__))
            registry.register(
                env_id="refund-env", env_version="refund-env/1",
                adapter_ref=f"{__name__}:BrokenRefundReset",
                display_name="Broken refund", declared_tools=refund_tools(),
                declared_modes=["recorded-actions", "deterministic-reset"])
            report = run_qualification(registry, "refund-env")
            self.assertEqual(report["status"], "failed")
            store, resolved_id = self._resolve(directory, registry)
            with self.assertRaisesRegex(ConfigError, "not qualified"):
                ops.start_run(store, registry, "proj-a", resolved_id)

    def test_execute_runs_qualified_env(self):
        with tempfile.TemporaryDirectory(
                prefix="booking-int-") as directory:
            registry = EnvRegistry(Path(directory) / "env.sqlite")
            registry.register(
                env_id="refund-env", env_version="refund-env/1",
                adapter_ref=REFUND_REF, display_name="Synthetic refund",
                declared_tools=refund_tools(),
                declared_modes=["recorded-actions", "deterministic-reset"])
            report = run_qualification(registry, "refund-env")
            self.assertEqual(report["status"], "qualified-for-declared")
            store, resolved_id = self._resolve(directory, registry)
            run = ops.start_run(store, registry, "proj-a", resolved_id)
            self.assertEqual(run["run"]["status"], "completed")
            self.assertEqual(run["run"]["verdict"], "fail")

    def test_execute_refuses_changed_adapter_code(self):
        with tempfile.TemporaryDirectory(
                prefix="booking-int-") as directory:
            registry = EnvRegistry(Path(directory) / "env.sqlite")
            registry.register(
                env_id="refund-env", env_version="refund-env/1",
                adapter_ref=REFUND_REF, display_name="Synthetic refund",
                declared_tools=refund_tools(),
                declared_modes=["recorded-actions", "deterministic-reset"])
            run_qualification(registry, "refund-env")
            store, resolved_id = self._resolve(directory, registry)
            connection = sqlite3.connect(Path(directory) / "env.sqlite")
            try:
                connection.execute(
                    "UPDATE env_registrations SET code_digest = ?"
                    " WHERE env_id = ?", ("0" * 64, "refund-env"))
                connection.commit()
            finally:
                connection.close()
            with self.assertRaisesRegex(ConfigError, "changed since"):
                ops.start_run(store, registry, "proj-a", resolved_id)


if __name__ == "__main__":
    unittest.main()
