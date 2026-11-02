"""Runnable Track E1 demo: naive duplicate, stable-key repair, revoke-mid-flow.

Runs a naive agent (fresh idempotency key per retry) and a corrected agent
(stable idempotency key) against the real loopback service through the
boundary proxy with ``timeout_after_commit`` armed, plus a revoke-mid-flow
run with ``permission_revoked`` armed. Prints a JSON summary and exits 0
only when every expectation holds. Standard library only, no model calls.
"""

import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from examples.booking_service.env_adapter import ProxyEnvAdapter  # noqa: E402
from examples.booking_service.service import DEFAULT_FIXTURE  # noqa: E402
from ghostlab.envs.adapters import EnvError  # noqa: E402


def _book(adapter, key):
    return adapter.invoke("book_equipment", {
        "equipment_id": DEFAULT_FIXTURE["equipment_id"],
        "user_id": DEFAULT_FIXTURE["user_id"],
        "slot": DEFAULT_FIXTURE["slot"],
        "idempotency_key": key,
    })


def main() -> int:
    summary: dict = {}
    ok = True
    adapter = ProxyEnvAdapter()
    try:
        # 1. Naive agent: timeout, then retry with a FRESH key -> duplicate.
        adapter.clear_faults()
        adapter.reset(deepcopy(DEFAULT_FIXTURE))
        adapter.set_fault("timeout_after_commit", {})
        try:
            _book(adapter, "naive-1")
            summary["naive_first"] = "unexpected-success"
            ok = False
        except EnvError as error:
            summary["naive_first_error"] = str(error)[:160]
            ok = ("504" in str(error)) and ok
        _book(adapter, "naive-2")
        naive_total = adapter.snapshot()["total_bookings"]
        summary["naive_total_bookings"] = naive_total
        summary["naive_transcript_len"] = len(adapter.transcript)
        ok = (naive_total == 2 and len(adapter.transcript) == 2) and ok

        # 2. Corrected agent: timeout, then retry with the SAME key -> one row.
        adapter.clear_faults()
        adapter.reset(deepcopy(DEFAULT_FIXTURE))
        adapter.set_fault("timeout_after_commit", {})
        try:
            _book(adapter, "stable-1")
            summary["stable_first"] = "unexpected-success"
            ok = False
        except EnvError as error:
            summary["stable_first_error"] = str(error)[:160]
            ok = ("504" in str(error)) and ok
        row = _book(adapter, "stable-1")
        stable_total = adapter.snapshot()["total_bookings"]
        summary["stable_total_bookings"] = stable_total
        summary["stable_reservation_id"] = row["reservation_id"]
        summary["stable_transcript_len"] = len(adapter.transcript)
        ok = (stable_total == 1 and len(adapter.transcript) == 2
              and row["reservation_id"] == "booking-1") and ok

        # 3. Revoke mid-flow: 403 without forwarding, no duplicate.
        adapter.clear_faults()
        adapter.reset(deepcopy(DEFAULT_FIXTURE))
        adapter.set_fault("permission_revoked", {})
        try:
            _book(adapter, "revoked-1")
            summary["revoked_first"] = "unexpected-success"
            ok = False
        except EnvError as error:
            summary["revoked_first_error"] = str(error)[:160]
            ok = ("403" in str(error)) and ok
        revoked_total = adapter.snapshot()["total_bookings"]
        transcript = adapter.transcript
        summary["revoked_total_bookings"] = revoked_total
        summary["revoked_transcript_len"] = len(transcript)
        summary["revoked_forwarded_upstream"] = any(
            entry["upstream_status"] is not None for entry in transcript)
        ok = (revoked_total == 0 and len(transcript) == 1
              and transcript[0]["upstream_status"] is None
              and transcript[0]["injected"] == "permission_revoked") and ok
    finally:
        adapter.close()
    summary["ok"] = ok
    print(json.dumps(summary, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
