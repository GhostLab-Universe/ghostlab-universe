"""Track E1: boundary-proxy fault injection over a real loopback service.

Covers the booking service (CRUD + reset), the boundary proxy (forwarding,
transcript, timeout-after-commit, revocation) and the proxy-backed adapter
(full 7-check qualification plus rejection/cancellation/fault validation).

Every registry lives in a temporary directory; servers bind 127.0.0.1 on
ephemeral ports and are stopped after each test.
"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.booking_service.env_adapter import (  # noqa: E402
    BOOKING_MODES, BOOKING_TOOLS, ProxyEnvAdapter)
from examples.booking_service.service import (  # noqa: E402
    DEFAULT_FIXTURE, BookingService)
from ghostlab import envs  # noqa: E402
from ghostlab.envs.adapters import EnvCancelled, EnvError  # noqa: E402
from ghostlab.envs.qualification import run_qualification  # noqa: E402
from ghostlab.envs.registry import EnvRegistry  # noqa: E402
from ghostlab.proxy import BoundaryProxy  # noqa: E402

FIXTURE = deepcopy(DEFAULT_FIXTURE)
ADAPTER_REF = "examples.booking_service.env_adapter:ProxyEnvAdapter"


def post(url, payload):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def get(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def book_args(key):
    return {"equipment_id": FIXTURE["equipment_id"], "user_id": FIXTURE["user_id"],
            "slot": FIXTURE["slot"], "idempotency_key": key}


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.service = BookingService()
        self.service.start()
        self.addCleanup(self.service.stop)

    def test_crud_and_reset(self):
        base = self.service.base_url
        status, row = post(base + "/book_equipment", book_args("k-1"))
        self.assertEqual(status, 200)
        self.assertEqual(row["reservation_id"], "booking-1")
        status, same = post(base + "/book_equipment", book_args("k-1"))
        self.assertEqual((status, same), (200, row))
        status, found = post(base + "/get_booking_status", {
            "equipment_id": FIXTURE["equipment_id"], "user_id": FIXTURE["user_id"],
            "idempotency_key": "k-1"})
        self.assertEqual(status, 200)
        self.assertEqual(found["status"], "committed")
        self.assertEqual(found["booking"], row)
        status, missing = post(base + "/get_booking_status", {
            "equipment_id": FIXTURE["equipment_id"], "user_id": FIXTURE["user_id"],
            "idempotency_key": "nope"})
        self.assertEqual(missing["status"], "not_found")
        status, equipment = post(base + "/get_equipment", {
            "equipment_id": FIXTURE["equipment_id"], "user_id": FIXTURE["user_id"]})
        self.assertEqual(status, 200)
        self.assertTrue(equipment["available"])
        status, state = get(base + "/state")
        self.assertEqual(state["total_bookings"], 1)
        status, _ = post(base + "/reset", deepcopy(FIXTURE))
        self.assertEqual(status, 200)
        _, state = get(base + "/state")
        self.assertEqual(state, {"equipment": FIXTURE, "reservations": [],
                                 "total_bookings": 0})
        _, row = post(base + "/book_equipment", book_args("k-2"))
        self.assertEqual(row["reservation_id"], "booking-1")

    def test_unknown_tool_rejected(self):
        status, body = post(self.service.base_url + "/no_such_tool", {})
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "UnknownTool")


class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.service = BookingService()
        self.service.start()
        self.addCleanup(self.service.stop)
        self.proxy = BoundaryProxy(self.service.base_url)
        self.proxy.start()
        self.addCleanup(self.proxy.stop)

    def test_forward_and_transcript(self):
        status, row = post(self.proxy.base_url + "/book_equipment", book_args("k-1"))
        self.assertEqual(status, 200)
        self.assertEqual(row["reservation_id"], "booking-1")
        transcript = self.proxy.transcript
        self.assertEqual(len(transcript), 1)
        entry = transcript[0]
        self.assertEqual(entry, {"seq": 1, "method": "POST",
                                 "path": "/book_equipment",
                                 "request": book_args("k-1"),
                                 "upstream_status": 200, "injected": None})
        _, state = get(self.service.base_url + "/state")
        self.assertEqual(state["total_bookings"], 1)

    def test_timeout_fires_once_on_kth_occurrence_then_disarms(self):
        self.proxy.arm_timeout_after_commit("/book_equipment", occurrence=2)
        base = self.proxy.base_url
        status, _ = post(base + "/book_equipment", book_args("k-1"))
        self.assertEqual(status, 200)
        status, body = post(base + "/book_equipment", book_args("k-2"))
        self.assertEqual(status, 504)
        self.assertEqual(body["error"], "TimeoutAfterCommit")
        _, state = get(self.service.base_url + "/state")
        self.assertEqual(state["total_bookings"], 2)  # commit really happened
        status, row = post(base + "/book_equipment", book_args("k-3"))
        self.assertEqual(status, 200)
        self.assertEqual(row["reservation_id"], "booking-3")
        self.assertEqual(self.proxy.fired_occurrence, 2)
        self.assertFalse(self.proxy.timeout_armed)
        injected = [entry["injected"] for entry in self.proxy.transcript]
        self.assertEqual(injected, [None, "timeout_after_commit", None])
        fired = self.proxy.transcript[1]
        self.assertEqual(fired["upstream_status"], 200)

    def test_revoke_blocks_mutation_without_forwarding(self):
        self.proxy.set_revoked(True)
        base = self.proxy.base_url
        status, body = post(base + "/book_equipment", book_args("k-1"))
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "PermissionRevoked")
        _, state = get(self.service.base_url + "/state")
        self.assertEqual(state["total_bookings"], 0)
        transcript = self.proxy.transcript
        self.assertEqual(len(transcript), 1)
        self.assertIsNone(transcript[0]["upstream_status"])
        self.assertEqual(transcript[0]["injected"], "permission_revoked")
        status, _ = post(base + "/get_equipment", {
            "equipment_id": FIXTURE["equipment_id"], "user_id": FIXTURE["user_id"]})
        self.assertEqual(status, 200)  # reads still forward while revoked
        self.assertEqual(self.proxy.transcript[1]["upstream_status"], 200)
        self.proxy.set_revoked(False)
        status, row = post(base + "/book_equipment", book_args("k-1"))
        self.assertEqual(status, 200)
        self.assertEqual(row["reservation_id"], "booking-1")


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.adapter = ProxyEnvAdapter()
        self.addCleanup(self.adapter.close)

    def test_describe_hooks_match_pattern(self):
        import re
        described = self.adapter.describe()
        self.assertTrue(described["fault_hooks"])
        for hook in described["fault_hooks"]:
            self.assertRegex(hook, r"^[a-z][a-z0-9_]{0,63}$")

    def test_unknown_tool_rejected_unmutated(self):
        self.adapter.reset(deepcopy(FIXTURE))
        before = self.adapter.snapshot()
        with self.assertRaises(EnvError):
            self.adapter.invoke("no_such_tool_xyz", {})
        tampered = {"equipment_id": FIXTURE["equipment_id"],
                    "user_id": FIXTURE["user_id"], "unexpected_arg_xyz": "1"}
        with self.assertRaises(EnvError):
            self.adapter.invoke("get_equipment", tampered)
        self.assertEqual(self.adapter.snapshot(), before)
        self.assertEqual(self.adapter.evidence()["trace"], [])
        self.assertEqual(self.adapter.transcript, [])

    def test_set_fault_validation(self):
        with self.assertRaises(EnvError):
            self.adapter.set_fault("no_such_hook_xyz", {})
        with self.assertRaises(ValueError):
            self.adapter.set_fault("timeout_after_commit", {"occurrence": 2})
        with self.assertRaises(ValueError):
            self.adapter.set_fault("permission_revoked", {"revoked": True})

    def test_cancellation(self):
        self.adapter.reset(deepcopy(FIXTURE))
        before = self.adapter.snapshot()
        probe = self.adapter.probe_calls(FIXTURE)[0]
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(EnvCancelled):
            self.adapter.invoke_cancellable(probe["tool"], probe["arguments"],
                                            cancelled)
        self.assertEqual(self.adapter.snapshot(), before)

    def test_revoked_fault_blocks_booking_without_duplicate(self):
        self.adapter.reset(deepcopy(FIXTURE))
        self.adapter.set_fault("permission_revoked", {})
        with self.assertRaises(EnvError) as raised:
            self.adapter.invoke("book_equipment", book_args("k-1"))
        self.assertIn("403", str(raised.exception))
        self.assertEqual(self.adapter.snapshot()["total_bookings"], 0)
        self.adapter.clear_faults()
        row = self.adapter.invoke("book_equipment", book_args("k-1"))
        self.assertEqual(row["reservation_id"], "booking-1")

    def test_qualifies_all_seven_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = EnvRegistry(
                Path(directory) / "env.sqlite",
                allowed_adapter_prefixes=("ghostlab.",
                                          "examples.booking_service."))
            registry.register(
                env_id="booking-proxy-env", env_version="booking-proxy-env/1",
                adapter_ref=ADAPTER_REF,
                display_name="Booking service via loopback proxy (synthetic upstream)",
                declared_tools=deepcopy(BOOKING_TOOLS),
                declared_modes=list(BOOKING_MODES))
            report = run_qualification(registry, "booking-proxy-env",
                                       fixture=deepcopy(FIXTURE))
        self.assertEqual(report["status"], envs.STATUS_QUALIFIED)
        by_name = {check["name"]: check for check in report["checks"]}
        self.assertEqual(set(by_name),
                         {"reset", "isolation", "schemas", "evidence",
                          "fault-hooks", "cancellation", "reproduction-modes"})
        for name, check in sorted(by_name.items()):
            self.assertTrue(check["passed"], f"{name}: {check['detail']}")


if __name__ == "__main__":
    unittest.main()
