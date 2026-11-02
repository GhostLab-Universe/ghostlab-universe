"""Version-bound qualification reports.

A report binds the verdict to the exact environment version, adapter
reference, reviewed-code digest, manifest digest, and runner version. Without
a stored qualification report an environment is ``not-qualified``.
"""

import hashlib
import json
from pathlib import Path

from . import STATUS_NOT_QUALIFIED, STATUSES
from .qualification import CHECKS

MAX_REPORT_BYTES = 1_000_000

_REPORT_KEYS = {"schema_version", "report_id", "env_id", "env_version",
                "adapter_ref", "code_digest", "manifest_digest", "runner_version",
                "checks", "status", "created_at"}
_CHECK_KEYS = {"name", "passed", "detail"}


def get_status(registry, env_id: str) -> str:
    """Latest qualification status, or not-qualified when never qualified."""
    registry.get(env_id)
    latest = registry.latest_report(env_id)
    return STATUS_NOT_QUALIFIED if latest is None else latest["status"]


def export_report(path: str | Path, report: dict) -> str:
    """Write a checksummed ``{payload, sha256}`` report envelope."""
    path = Path(path)
    payload = {"schema_version": 1, **{k: v for k, v in report.items()
                                      if k != "schema_version"}}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)
    digest = hashlib.sha256(text.encode()).hexdigest()
    envelope = {"payload": payload, "sha256": digest}
    out = json.dumps(envelope, indent=2) + "\n"
    if len(out.encode()) > MAX_REPORT_BYTES:
        raise ValueError("report exceeds the size bound")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(out, encoding="utf-8")
    return digest


def load_report(path: str | Path) -> dict:
    """Load and validate a report envelope; checksum detects tampering."""
    path = Path(path)
    if path.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("report exceeds the size bound")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    if type(envelope) is not dict or set(envelope) != {"payload", "sha256"}:
        raise ValueError("invalid report envelope")
    payload = envelope["payload"]
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)
    if hashlib.sha256(text.encode()).hexdigest() != envelope["sha256"]:
        raise ValueError("report checksum mismatch")
    if type(payload) is not dict or set(payload) != _REPORT_KEYS:
        raise ValueError("invalid report payload")
    if payload["status"] not in STATUSES or payload["status"] == STATUS_NOT_QUALIFIED:
        raise ValueError("stored reports carry qualified-for-declared or failed")
    checks = payload["checks"]
    if (type(checks) is not list or [c.get("name") for c in checks
                                    if type(c) is dict] != list(CHECKS)):
        raise ValueError("report must carry every qualification check in order")
    for check in checks:
        if (set(check) != _CHECK_KEYS or type(check["passed"]) is not bool
                or type(check["detail"]) is not str):
            raise ValueError("invalid report check entry")
    return payload
