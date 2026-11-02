"""Track E3: fidelity manifests plus the divergence harness.

Every test uses in-memory adapters or temporary directories; no live store
file is touched and no model is called.
"""

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.booking_service.env_adapter import ProxyEnvAdapter  # noqa: E402
from examples.equipment_booking.env_adapter import BookingEnvAdapter  # noqa: E402
from ghostlab.envs.adapters import RefundEnvAdapter  # noqa: E402
from ghostlab.fidelity.diverge import compare_runs, fidelity_probe  # noqa: E402
from ghostlab.fidelity.manifests import (ASSESSED_MANIFESTS,  # noqa: E402
                                         BOOKING_FIDELITY_MANIFEST,
                                         PROXY_BOOKING_FIDELITY_MANIFEST,
                                         PROXY_FIDELITY_TEMPLATE,
                                         REFUND_FIDELITY_MANIFEST,
                                         FidelityStore,
                                         validate_fidelity_manifest)

REFUND_FIXTURE = {
    "order_id": "synthetic-order-1",
    "customer_id": "synthetic-customer-1",
    "paid_minor": 250000,
    "currency": "INR",
}


def refund_actions():
    identity = {"order_id": REFUND_FIXTURE["order_id"],
                "customer_id": REFUND_FIXTURE["customer_id"]}
    return [
        {"tool": "get_order", "arguments": dict(identity)},
        {"tool": "issue_refund", "arguments": {
            **identity, "amount_minor": REFUND_FIXTURE["paid_minor"],
            "idempotency_key": "e3-intent-1"}},
    ]


def armed_refund_adapter():
    adapter = RefundEnvAdapter()
    adapter.set_fault("timeout_after_commit", {})
    return adapter


class _StubAdapter:
    """Minimal adapter with configurable tools for mismatch tests."""

    def __init__(self, tools):
        self._tools = tools
        self._calls = []

    def describe(self):
        return {"env_id": "stub", "version": "stub/1", "tools": self._tools,
                "reproduction_modes": ["recorded-actions"], "fault_hooks": []}

    def reset(self, fixture):
        self._calls = []

    def invoke(self, tool, arguments):
        names = {entry["name"] for entry in self._tools}
        if tool not in names:
            raise RuntimeError("Unknown tool")
        self._calls.append(tool)
        return {"echo": tool}

    def snapshot(self):
        return {"calls": list(self._calls)}

    def evidence(self):
        return {"snapshot": self.snapshot(),
                "trace": [{"call": name} for name in self._calls]}


class ManifestValidationTests(unittest.TestCase):
    def test_unknown_field_rejected(self):
        manifest = deepcopy(REFUND_FIDELITY_MANIFEST)
        manifest["homepage"] = "notes-only"
        with self.assertRaisesRegex(ValueError, "exactly the v1 fields"):
            validate_fidelity_manifest(manifest)

    def test_boundary_real_without_upstream_rejected(self):
        manifest = deepcopy(REFUND_FIDELITY_MANIFEST)
        fault = manifest["faults"]["timeout_after_commit"]
        fault["class"] = "boundary-real"
        fault["basis"] = ("Adapter forwards each call to the upstream "
                          "service; upstream executes the call.")
        fault["upstream"] = None
        with self.assertRaisesRegex(ValueError, "non-empty upstream"):
            validate_fidelity_manifest(manifest)

    def test_boundary_real_basis_must_name_forwarding(self):
        manifest = deepcopy(REFUND_FIDELITY_MANIFEST)
        fault = manifest["faults"]["timeout_after_commit"]
        fault["class"] = "boundary-real"
        fault["basis"] = "The real service handles this fault identically."
        fault["upstream"] = "upstream reservation service"
        with self.assertRaisesRegex(ValueError, "forwarding mechanism"):
            validate_fidelity_manifest(manifest)

    def test_bad_class_rejected(self):
        manifest = deepcopy(REFUND_FIDELITY_MANIFEST)
        manifest["faults"]["timeout_after_commit"]["class"] = "production"
        with self.assertRaisesRegex(ValueError, "unknown class"):
            validate_fidelity_manifest(manifest)

    def test_bad_state_basis_rejected(self):
        manifest = deepcopy(REFUND_FIDELITY_MANIFEST)
        manifest["state_basis"] = "production"
        with self.assertRaisesRegex(ValueError, "state_basis"):
            validate_fidelity_manifest(manifest)

    def test_url_and_path_rejected(self):
        manifest = deepcopy(REFUND_FIDELITY_MANIFEST)
        manifest["faults"]["timeout_after_commit"]["basis"] = (
            "See https://example.invalid/fault for the forwarding design.")
        with self.assertRaisesRegex(ValueError, "URL"):
            validate_fidelity_manifest(manifest)

    def test_shipped_manifests_are_honest_synthetic(self):
        self.assertEqual(len(ASSESSED_MANIFESTS), 2)
        for manifest in ASSESSED_MANIFESTS:
            validated = validate_fidelity_manifest(manifest)
            self.assertEqual(validated["state_basis"], "synthetic-model")
            for fault in validated["faults"].values():
                self.assertEqual(fault["class"], "synthetic-only")
                self.assertIsNone(fault["upstream"])
        self.assertEqual(REFUND_FIDELITY_MANIFEST["env_id"], "refund-env")
        self.assertEqual(BOOKING_FIDELITY_MANIFEST["env_id"], "booking-env")

    def test_proxy_template_is_boundary_real(self):
        validated = validate_fidelity_manifest(PROXY_FIDELITY_TEMPLATE)
        self.assertEqual(validated["state_basis"], "authoritative-service")
        fault = validated["faults"]["timeout_after_commit"]
        self.assertEqual(fault["class"], "boundary-real")
        self.assertTrue(fault["upstream"])

    def test_proxy_booking_manifest_is_boundary_real(self):
        validated = validate_fidelity_manifest(
            PROXY_BOOKING_FIDELITY_MANIFEST)
        self.assertEqual(validated["env_id"], "booking-proxy-env")
        self.assertEqual(validated["env_version"], "booking-proxy-env/1")
        self.assertEqual(
            validated["adapter_ref"],
            "examples.booking_service.env_adapter:ProxyEnvAdapter")
        self.assertEqual(validated["state_basis"], "authoritative-service")
        self.assertEqual(set(validated["faults"]),
                         {"timeout_after_commit", "permission_revoked"})
        for fault in validated["faults"].values():
            self.assertEqual(fault["class"], "boundary-real")
            # Boundary-real mechanism, synthetic upstream: both stated.
            self.assertIn("synthetic", fault["upstream"])
        with tempfile.TemporaryDirectory(prefix="e3-fidelity-") as directory:
            store = FidelityStore(Path(directory))
            store.save(PROXY_BOOKING_FIDELITY_MANIFEST)
            self.assertEqual(store.load("booking-proxy-env"),
                             PROXY_BOOKING_FIDELITY_MANIFEST)


class FidelityStoreTests(unittest.TestCase):
    def test_roundtrip_and_list(self):
        with tempfile.TemporaryDirectory(prefix="e3-fidelity-") as directory:
            store = FidelityStore(Path(directory))
            for manifest in ASSESSED_MANIFESTS:
                saved = store.save(manifest)
                self.assertEqual(saved["env_id"], manifest["env_id"])
            loaded = store.load("refund-env")
            self.assertEqual(loaded, REFUND_FIDELITY_MANIFEST)
            rows = store.list()
            self.assertEqual([row["env_id"] for row in rows],
                             ["booking-env", "refund-env"])
            one = store.list("booking-env")
            self.assertEqual(len(one), 1)
            self.assertEqual(one[0]["env_id"], "booking-env")

    def test_tamper_detected(self):
        with tempfile.TemporaryDirectory(prefix="e3-fidelity-") as directory:
            store = FidelityStore(Path(directory))
            store.save(REFUND_FIDELITY_MANIFEST)
            path = Path(directory) / "refund-env.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["manifest"]["state_basis"] = "authoritative-service"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "tamper"):
                store.load("refund-env")


class CompareRunsTests(unittest.TestCase):
    def test_self_compare_clean(self):
        result = compare_runs(RefundEnvAdapter, RefundEnvAdapter,
                              REFUND_FIXTURE, refund_actions())
        self.assertTrue(result["comparable"])
        self.assertIsNone(result["reason"])
        self.assertFalse(result["diverged"])
        self.assertIsNone(result["first_divergence_step"])
        self.assertEqual(result["diffs"], [])

    def test_armed_vs_clean_diverges_at_step_two(self):
        result = compare_runs(armed_refund_adapter, RefundEnvAdapter,
                              REFUND_FIXTURE, refund_actions())
        self.assertTrue(result["comparable"])
        self.assertTrue(result["diverged"])
        self.assertEqual(result["first_divergence_step"], 2)
        kinds = {entry["kind"] for entry in result["diffs"]
                 if entry["step"] == 2}
        self.assertIn("outcome", kinds)
        self.assertLessEqual(len(result["diffs"]), 50)

    def test_refund_vs_booking_incomparable(self):
        result = compare_runs(RefundEnvAdapter, BookingEnvAdapter,
                              REFUND_FIXTURE, refund_actions())
        self.assertFalse(result["comparable"])
        self.assertTrue(result["reason"])
        self.assertFalse(result["diverged"])
        self.assertIsNone(result["first_divergence_step"])

    def test_tool_set_mismatch_incomparable(self):
        tools_a = [{"name": "ping", "parameters": []}]
        tools_b = [{"name": "pong", "parameters": []}]
        result = compare_runs(lambda: _StubAdapter(tools_a),
                              lambda: _StubAdapter(tools_b),
                              {"any": "fixture"},
                              [{"tool": "ping", "arguments": {}}])
        self.assertFalse(result["comparable"])
        self.assertIn("tool sets differ", result["reason"])

    def test_proxy_and_direct_share_state_transitions(self):
        fixture = {"equipment_id": "synthetic-microscope-1",
                   "user_id": "synthetic-user-1",
                   "slot": "2026-11-03T09:00Z"}
        identity = {"equipment_id": fixture["equipment_id"],
                    "user_id": fixture["user_id"]}
        actions = [
            {"tool": "get_equipment", "arguments": dict(identity)},
            {"tool": "book_equipment", "arguments": {
                **identity, "slot": fixture["slot"],
                "idempotency_key": "e3-fidelity-1"}},
        ]
        result = compare_runs(ProxyEnvAdapter, BookingEnvAdapter,
                              fixture, actions)
        self.assertTrue(result["comparable"], result["reason"])
        # State transitions agree step by step; only the trace envelopes
        # differ (the proxy trace carries extra boundary provenance).
        snapshot_diffs = [entry for entry in result["diffs"]
                          if entry["kind"] == "snapshot"]
        self.assertEqual(snapshot_diffs, [])


class FidelityProbeTests(unittest.TestCase):
    def test_probe_stable_clean(self):
        result = fidelity_probe(RefundEnvAdapter, REFUND_FIXTURE,
                                refund_actions())
        self.assertTrue(result["deterministic"])
        self.assertEqual(result["replays"], 2)
        self.assertEqual(result["steps"], 2)
        self.assertIsNone(result["first_nondeterminism_step"])
        self.assertEqual([entry["step"] for entry in result["transitions"]],
                         [1, 2])
        self.assertEqual(result["transitions"][0]["tool"], "get_order")
        self.assertTrue(result["final_snapshot_digest"])

    def test_probe_stable_armed(self):
        result = fidelity_probe(armed_refund_adapter, REFUND_FIXTURE,
                                refund_actions())
        self.assertTrue(result["deterministic"])
        self.assertEqual(result["transitions"][1]["outcome"],
                         "error:PaymentTimeout")


if __name__ == "__main__":
    unittest.main()
