"""Track A: registration, manifest import, discovery, qualification, reports.

Every test uses a temporary database; no live registry file is touched.
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from threading import Event

from ghostlab.core import PaymentTimeout
from ghostlab.envs.adapters import (REFUND_MODES, REFUND_TOOLS, EnvCancelled,
                                    EnvError, RefundEnvAdapter)
from ghostlab.envs.capabilities import discover
from ghostlab.envs.cli import main as env_main
from ghostlab.envs.manifest import (MAX_MANIFEST_BYTES, import_manifest,
                                    validate_manifest_bytes)
from ghostlab.envs.qualification import (CHECKS, DEFAULT_FIXTURE,
                                         run_qualification)
from ghostlab.envs.registry import EnvRegistry
from ghostlab.envs.reports import export_report, get_status, load_report

REFUND_REF = "ghostlab.envs.adapters:RefundEnvAdapter"


class CountingAdapter(RefundEnvAdapter):
    env_id = "counting"
    version = "counting/1"
    inits = describes = resets = invokes = 0

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

    @classmethod
    def reset_counters(cls):
        cls.inits = cls.describes = cls.resets = cls.invokes = 0


class BrokenResetAdapter(RefundEnvAdapter):
    def reset(self, fixture):
        raise RuntimeError("boom")


class NoCancelAdapter(RefundEnvAdapter):
    def describe(self):
        described = super().describe()
        described["supports_cancellation"] = False
        return described


class NoHooksAdapter(RefundEnvAdapter):
    def describe(self):
        described = super().describe()
        described["fault_hooks"] = []
        return described


class IncompleteAdapter:
    def describe(self):
        return {}


def make_registry(directory, *extra_prefixes):
    return EnvRegistry(Path(directory) / "test-env.sqlite",
                       allowed_adapter_prefixes=("ghostlab.",) + extra_prefixes)


def register_refund(registry, env_id="refund-env"):
    return registry.register(
        env_id=env_id, env_version="refund-env/1", adapter_ref=REFUND_REF,
        display_name="Synthetic refund", declared_tools=deepcopy(REFUND_TOOLS),
        declared_modes=list(REFUND_MODES))


def valid_manifest(env_id="refund-env", **overrides):
    manifest = {
        "manifest_version": 1, "env_id": env_id,
        "env_version": "refund-env/1", "adapter_ref": REFUND_REF,
        "display_name": "Synthetic refund",
        "tools": deepcopy(REFUND_TOOLS),
        "reproduction_modes": list(REFUND_MODES),
        "fault_hooks": ["timeout_after_commit"], "notes": "reviewed local adapter",
    }
    manifest.update(overrides)
    return manifest


def manifest_bytes(manifest):
    return json.dumps(manifest).encode()


def run_cli(args):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = env_main(args)
    return code, json.loads(buffer.getvalue())


class RegistrationTests(unittest.TestCase):
    def test_register_list_get(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            record = register_refund(registry)
            self.assertEqual(record["status"], "not-qualified")
            self.assertRegex(record["code_digest"], r"^[0-9a-f]{64}$")
            self.assertEqual([e["env_id"] for e in registry.list_envs()],
                             ["refund-env"])
            self.assertEqual(registry.get("refund-env")["adapter_ref"], REFUND_REF)

    def test_duplicate_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            with self.assertRaisesRegex(ValueError, "already registered"):
                register_refund(registry)

    def test_bad_identifiers_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            for env_id, version in [("Refund!", "refund-env/1"), ("", "refund-env/1"),
                                    ("refund-env", ""), ("refund-env", "bad version")]:
                with self.subTest(env_id=env_id, version=version):
                    with self.assertRaises(ValueError):
                        registry.register(
                            env_id=env_id, env_version=version,
                            adapter_ref=REFUND_REF, display_name="x",
                            declared_tools=deepcopy(REFUND_TOOLS),
                            declared_modes=list(REFUND_MODES))

    def test_nonallowlisted_and_missing_adapters_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            for ref in ("os:system", "collections:OrderedDict", "no-colon-here",
                        "ghostlab.envs.adapters:Missing",
                        "ghostlab.nonexistent_xyz:Nope"):
                with self.subTest(ref=ref), self.assertRaises(ValueError):
                    registry.register(
                        env_id="refund-env", env_version="refund-env/1",
                        adapter_ref=ref, display_name="x",
                        declared_tools=deepcopy(REFUND_TOOLS),
                        declared_modes=list(REFUND_MODES))

    def test_incomplete_adapter_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory, __name__)
            with self.assertRaisesRegex(ValueError, "misses"):
                registry.register(
                    env_id="partial", env_version="partial/1",
                    adapter_ref=f"{__name__}:IncompleteAdapter",
                    display_name="x", declared_tools=deepcopy(REFUND_TOOLS),
                    declared_modes=list(REFUND_MODES))

    def test_default_allowlist_rejects_test_module(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            with self.assertRaisesRegex(ValueError, "allowlist"):
                registry.register(
                    env_id="counting", env_version="counting/1",
                    adapter_ref=f"{__name__}:CountingAdapter", display_name="x",
                    declared_tools=deepcopy(REFUND_TOOLS),
                    declared_modes=list(REFUND_MODES))


class ManifestTests(unittest.TestCase):
    def test_import_valid_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            summary = import_manifest(registry, manifest_bytes(valid_manifest()))
            self.assertRegex(summary["manifest_digest"], r"^[0-9a-f]{64}$")
            stored = registry.get_manifest(summary["manifest_digest"])
            self.assertEqual(stored["manifest"]["env_id"], "refund-env")
            self.assertEqual(len(registry.list_manifests("refund-env")), 1)

    def test_import_requires_registration_and_exact_match(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            with self.assertRaisesRegex(ValueError, "unknown env_id"):
                import_manifest(registry, manifest_bytes(valid_manifest()))
            register_refund(registry)
            for field, value in [("env_version", "refund-env/2"),
                                 ("adapter_ref", "ghostlab.envs.adapters:Other")]:
                with self.subTest(field=field):
                    with self.assertRaisesRegex(ValueError, "does not match"):
                        import_manifest(
                            registry, manifest_bytes(valid_manifest(**{field: value})))

    def test_oversize_and_malformed_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            with self.assertRaisesRegex(ValueError, "size bound"):
                import_manifest(registry, b"x" * (MAX_MANIFEST_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                import_manifest(registry, b"{nope")

    def test_unknown_keys_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            for extra in ("pip_packages", "code_url", "path", "install", "url"):
                manifest = valid_manifest()
                manifest[extra] = ["x"]
                with self.subTest(extra=extra):
                    with self.assertRaisesRegex(ValueError, "exactly the v1 fields"):
                        import_manifest(registry, manifest_bytes(manifest))

    def test_urls_and_paths_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            bad_notes = ["see https://example.com/x", "fetch http://x.invalid",
                         "file:///etc/passwd", "/etc/passwd", "C:\\Windows\\x",
                         "\\\\server\\share"]
            for notes in bad_notes:
                with self.subTest(notes=notes):
                    with self.assertRaises(ValueError):
                        import_manifest(
                            registry, manifest_bytes(valid_manifest(notes=notes)))

    def test_validation_is_pure(self):
        manifest = valid_manifest(adapter_ref="ghostlab.nonexistent_xyz:Nope",
                                  env_id="ghost-env")
        parsed = validate_manifest_bytes(manifest_bytes(manifest))
        self.assertEqual(parsed["env_id"], "ghost-env")
        self.assertNotIn("ghostlab.nonexistent_xyz", sys.modules)
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            with self.assertRaisesRegex(ValueError, "unknown env_id"):
                import_manifest(registry, manifest_bytes(manifest))


class NoExecutionTests(unittest.TestCase):
    def test_import_never_instantiates(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory, __name__)
            CountingAdapter.reset_counters()
            registry.register(
                env_id="counting", env_version="counting/1",
                adapter_ref=f"{__name__}:CountingAdapter", display_name="x",
                declared_tools=deepcopy(REFUND_TOOLS),
                declared_modes=list(REFUND_MODES))
            CountingAdapter.reset_counters()
            manifest = valid_manifest(env_id="counting", env_version="counting/1",
                                      adapter_ref=f"{__name__}:CountingAdapter")
            validate_manifest_bytes(manifest_bytes(manifest))
            import_manifest(registry, manifest_bytes(manifest))
            self.assertEqual((CountingAdapter.inits, CountingAdapter.describes,
                              CountingAdapter.resets, CountingAdapter.invokes),
                             (0, 0, 0, 0))

    def test_discovery_calls_describe_only(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory, __name__)
            registry.register(
                env_id="counting", env_version="counting/1",
                adapter_ref=f"{__name__}:CountingAdapter", display_name="x",
                declared_tools=deepcopy(REFUND_TOOLS),
                declared_modes=list(REFUND_MODES))
            CountingAdapter.reset_counters()
            result = discover(registry, "counting")
            self.assertTrue(result["match"])
            self.assertEqual(CountingAdapter.inits, 1)
            self.assertEqual(CountingAdapter.describes, 1)
            self.assertEqual(CountingAdapter.resets, 0)
            self.assertEqual(CountingAdapter.invokes, 0)


class CapabilityTests(unittest.TestCase):
    def test_discover_match(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            result = discover(registry, "refund-env")
            self.assertTrue(result["match"])
            self.assertEqual(result["mismatches"], [])
            self.assertEqual(result["discovered_tools"], deepcopy(REFUND_TOOLS))
            self.assertEqual(result["fault_hooks"], ["timeout_after_commit"])
            self.assertTrue(result["supports_cancellation"])
            self.assertIsNone(result["manifest_digest"])

    def test_discover_with_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            summary = import_manifest(registry, manifest_bytes(valid_manifest()))
            result = discover(registry, "refund-env",
                              manifest_digest=summary["manifest_digest"])
            self.assertTrue(result["match"])
            self.assertEqual(result["manifest_digest"], summary["manifest_digest"])

    def test_discover_mismatch_noted(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            registry.register(
                env_id="refund-env", env_version="refund-env/1",
                adapter_ref=REFUND_REF, display_name="x",
                declared_tools=[{"name": "get_order",
                                 "parameters": ["order_id", "customer_id"]}],
                declared_modes=["recorded-actions"])
            result = discover(registry, "refund-env")
            self.assertFalse(result["match"])
            self.assertIn("tools", result["mismatches"])
            self.assertIn("reproduction_modes", result["mismatches"])

    def test_discover_rejects_foreign_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            register_refund(registry, env_id="second-env")
            manifest = valid_manifest(env_id="second-env")
            summary = import_manifest(registry, manifest_bytes(manifest))
            with self.assertRaisesRegex(ValueError, "different env_id"):
                discover(registry, "refund-env",
                         manifest_digest=summary["manifest_digest"])


class QualificationTests(unittest.TestCase):
    def test_qualify_refund_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            record = register_refund(registry)
            summary = import_manifest(registry, manifest_bytes(valid_manifest()))
            report = run_qualification(
                registry, "refund-env",
                manifest_digest=summary["manifest_digest"])
            self.assertEqual(report["status"], "qualified-for-declared")
            self.assertEqual([c["name"] for c in report["checks"]], list(CHECKS))
            self.assertTrue(all(c["passed"] for c in report["checks"]))
            self.assertEqual(report["env_version"], "refund-env/1")
            self.assertEqual(report["adapter_ref"], REFUND_REF)
            self.assertEqual(report["code_digest"], record["code_digest"])
            self.assertEqual(report["manifest_digest"], summary["manifest_digest"])
            self.assertEqual(report["runner_version"], "env-qualification/1")
            self.assertEqual(registry.get("refund-env")["status"],
                             "qualified-for-declared")
            self.assertEqual(get_status(registry, "refund-env"),
                             "qualified-for-declared")

    def test_qualify_without_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            report = run_qualification(registry, "refund-env")
            self.assertEqual(report["status"], "qualified-for-declared")
            self.assertIsNone(report["manifest_digest"])

    def _register_test_adapter(self, registry, name, cls_name):
        return registry.register(
            env_id=name, env_version="test/1",
            adapter_ref=f"{__name__}:{cls_name}", display_name="x",
            declared_tools=deepcopy(REFUND_TOOLS),
            declared_modes=list(REFUND_MODES))

    def test_broken_reset_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory, __name__)
            self._register_test_adapter(registry, "broken", "BrokenResetAdapter")
            report = run_qualification(registry, "broken")
            self.assertEqual(report["status"], "failed")
            reset = next(c for c in report["checks"] if c["name"] == "reset")
            self.assertFalse(reset["passed"])
            self.assertIn("boom", reset["detail"])
            self.assertEqual(get_status(registry, "broken"), "failed")

    def test_no_cancel_fails_only_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory, __name__)
            self._register_test_adapter(registry, "nocancel", "NoCancelAdapter")
            report = run_qualification(registry, "nocancel")
            self.assertEqual(report["status"], "failed")
            by_name = {c["name"]: c["passed"] for c in report["checks"]}
            self.assertFalse(by_name["cancellation"])
            self.assertTrue(by_name["reset"] and by_name["schemas"])

    def test_no_hooks_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory, __name__)
            self._register_test_adapter(registry, "nohooks", "NoHooksAdapter")
            report = run_qualification(registry, "nohooks")
            self.assertEqual(report["status"], "failed")
            hooks = next(c for c in report["checks"] if c["name"] == "fault-hooks")
            self.assertFalse(hooks["passed"])

    def test_qualification_cancelled_stores_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            cancel = Event()
            cancel.set()
            with self.assertRaises(EnvCancelled):
                run_qualification(registry, "refund-env", cancel=cancel)
            self.assertIsNone(registry.latest_report("refund-env"))
            self.assertEqual(get_status(registry, "refund-env"), "not-qualified")


class RefundPreservationTests(unittest.TestCase):
    def test_adapter_baseline_total(self):
        adapter = RefundEnvAdapter()
        adapter.reset(deepcopy(DEFAULT_FIXTURE))
        identity = {"order_id": "synthetic-order-1",
                    "customer_id": "synthetic-customer-1"}
        adapter.invoke("get_order", identity)
        adapter.invoke("issue_refund", {**identity, "amount_minor": 250_000,
                                        "idempotency_key": "k1"})
        self.assertEqual(adapter.snapshot()["total_refunded_minor"], 250_000)

    def test_adapter_fault_timeout_then_retry(self):
        adapter = RefundEnvAdapter()
        adapter.set_fault("timeout_after_commit", {})
        adapter.reset(deepcopy(DEFAULT_FIXTURE))
        identity = {"order_id": "synthetic-order-1",
                    "customer_id": "synthetic-customer-1"}
        with self.assertRaises(PaymentTimeout):
            adapter.invoke("issue_refund", {**identity, "amount_minor": 250_000,
                                            "idempotency_key": "k1"})
        self.assertEqual(adapter.snapshot()["total_refunded_minor"], 250_000)
        status = adapter.invoke("get_refund_status", {**identity,
                                                      "idempotency_key": "k1"})
        self.assertEqual(status["status"], "committed")
        adapter.invoke("issue_refund", {**identity, "amount_minor": 250_000,
                                        "idempotency_key": "k2"})
        self.assertEqual(adapter.snapshot()["total_refunded_minor"], 500_000)
        adapter.clear_faults()
        adapter.reset(deepcopy(DEFAULT_FIXTURE))
        adapter.invoke("issue_refund", {**identity, "amount_minor": 250_000,
                                        "idempotency_key": "k1"})
        self.assertEqual(adapter.snapshot()["total_refunded_minor"], 250_000)

    def test_adapter_rejects_bad_hooks_and_early_invoke(self):
        adapter = RefundEnvAdapter()
        with self.assertRaises(EnvError):
            adapter.invoke("get_order", {"order_id": "x", "customer_id": "y"})
        with self.assertRaises(EnvError):
            adapter.set_fault("nope", {})
        with self.assertRaises(ValueError):
            adapter.set_fault("timeout_after_commit", {"x": 1})


class ReportTests(unittest.TestCase):
    def test_status_default_and_export_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            register_refund(registry)
            self.assertEqual(get_status(registry, "refund-env"), "not-qualified")
            report = run_qualification(registry, "refund-env")
            path = Path(directory) / "report.json"
            export_report(path, report)
            loaded = load_report(path)
            self.assertEqual(loaded["status"], "qualified-for-declared")
            self.assertEqual(loaded["checks"], report["checks"])
            envelope = json.loads(path.read_text())
            envelope["payload"]["checks"][0]["passed"] = False
            path.write_text(json.dumps(envelope))
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_report(path)


class CliTests(unittest.TestCase):
    def test_cli_full_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "cli.sqlite")
            code, record = run_cli([
                "env-register", "--db", db, "--env-id", "refund-env",
                "--env-version", "refund-env/1", "--adapter-ref", REFUND_REF,
                "--display-name", "Synthetic refund",
                "--tools", json.dumps(REFUND_TOOLS),
                "--modes", json.dumps(list(REFUND_MODES))])
            self.assertEqual(code, 0)
            self.assertEqual(record["status"], "not-qualified")
            code, listed = run_cli(["env-list", "--db", db])
            self.assertEqual(code, 0)
            self.assertEqual([e["env_id"] for e in listed["envs"]], ["refund-env"])
            manifest_path = Path(directory) / "manifest.json"
            manifest_path.write_text(json.dumps(valid_manifest()))
            code, summary = run_cli(
                ["env-import-manifest", "--db", db, "--manifest",
                 str(manifest_path)])
            self.assertEqual(code, 0)
            code, found = run_cli(["env-capabilities", "--db", db, "--env-id",
                                   "refund-env", "--manifest-digest",
                                   summary["manifest_digest"]])
            self.assertEqual(code, 0)
            self.assertTrue(found["match"])
            code, report = run_cli(["env-qualify", "--db", db, "--env-id",
                                    "refund-env", "--manifest-digest",
                                    summary["manifest_digest"]])
            self.assertEqual(code, 0)
            self.assertEqual(report["status"], "qualified-for-declared")
            code, latest = run_cli(["env-report", "--db", db, "--env-id",
                                    "refund-env"])
            self.assertEqual(code, 0)
            self.assertEqual(latest["report_id"], report["report_id"])

    def test_cli_unknown_env_exits_2(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "cli.sqlite")
            with self.assertRaises(SystemExit) as raised:
                run_cli(["env-report", "--db", db, "--env-id", "missing"])
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
