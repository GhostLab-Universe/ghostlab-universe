"""Build a release record: ``ghostlab-release/1``. Evidence for a human, not a deployment.

Runtime, dependency and live-capability statuses stay separate, because passing one says
nothing about the others. Offline evaluations can never move the live status: that takes
a sanitized M01 compatibility report from an authorized live run.

Nothing here deploys, merges or tags. ``promotable: false`` exits non-zero.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from .gate import check

SCHEMA = "ghostlab-release/1"
TARGETS_SCHEMA = "ghostlab-release-targets/1"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
LIVE_STATUSES = ("not_started", "authorized", "passed", "failed")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def target_problems(targets: dict) -> list[str]:
    problems = []
    if targets.get("schema") != TARGETS_SCHEMA:
        problems.append("unknown targets schema")
    rollback = targets.get("rollback_target") or {}
    if not _COMMIT.match(str(rollback.get("commit"))):
        problems.append("no explicit rollback commit")
    if not rollback.get("store_action"):
        problems.append("the rollback target doesn't say what happens to the stores")
    statuses = targets.get("statuses") or {}
    for name in ("runtime", "dependency", "live_capability"):
        if name not in statuses:
            problems.append(f"missing {name} status")
    live = statuses.get("live_capability") or {}
    if live.get("status") not in LIVE_STATUSES:
        problems.append("unknown live status")
    if live.get("status") == "passed" and not live.get("compatibility_report"):
        problems.append("live status can't be passed without a compatibility report")
    return problems


def build_record(*, report: dict, expected: dict, targets: dict, commit: str, root: Path) -> dict:
    blockers = [*check(report, expected), *target_problems(targets)]
    if not _COMMIT.match(commit):
        blockers.append("the candidate must be a full commit id")
    elif commit == (targets.get("rollback_target") or {}).get("commit"):
        blockers.append("the candidate equals its own rollback target")
    statuses = targets.get("statuses") or {}
    live = statuses.get("live_capability") or {}
    return {
        "schema": SCHEMA, "candidate_commit": commit,
        "release_target": targets.get("release_target"),
        "rollback_target": targets.get("rollback_target"),
        "evaluation": {"schema": report.get("schema"), "inference": report.get("inference"),
                       "cases": (report.get("aggregate") or {}).get("cases"),
                       "report_sha256": hashlib.sha256(json.dumps(
                           report, sort_keys=True).encode()).hexdigest()},
        "runtime": statuses.get("runtime"),
        "dependency": {**(statuses.get("dependency") or {}),
                       "uv_lock_sha256": _sha256(root / "uv.lock"),
                       "web_lock_sha256": _sha256(root / "web" / "package-lock.json")},
        "live_capability": live,
        "customer_connected_use_allowed": live.get("status") == "passed" and not blockers,
        "blockers": blockers, "promotable": not blockers}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Assemble release evidence for a human review")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--expected", type=Path, default=Path("evals/expected.json"))
    parser.add_argument("--targets", type=Path, default=Path("deploy/release-targets.json"))
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    record = build_record(report=json.loads(args.report.read_text("utf-8")),
                          expected=json.loads(args.expected.read_text("utf-8")),
                          targets=json.loads(args.targets.read_text("utf-8")),
                          commit=args.commit, root=args.root)
    print(json.dumps(record, indent=2))
    return 0 if record["promotable"] else 1


if __name__ == "__main__":
    sys.exit(main())
