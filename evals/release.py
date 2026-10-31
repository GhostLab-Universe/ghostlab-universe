"""Build a release record: ``ghostlab-release/1``. Evidence for a human, not a deployment.

Runtime, dependency and live-capability statuses stay separate, because passing one says
nothing about the others. Offline evaluations can never move the live status: that takes
a sanitized M01 compatibility report from an authorized live run.

A status in the targets file is a claim. ``promotable`` is about offline promotion only.
``customer_connected_use_allowed`` is decided separately and needs all of this:

- a target of kind ``customer_connected``, so a local target can never allow it;
- an authenticated runtime, and every security gate marked passed with an evidence file;
- a compatibility report that exists, is inside the release root and passes the same
  check the controller applies before a paid call. A path in a file proves nothing.

Nothing here deploys, merges or tags. ``promotable: false`` exits non-zero.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from ghostlab.observability import find_secrets
from ghostlab.orchestration import DEFAULT_WORLDS
from ghostlab.orchestration.inference import LiveInferenceAuthorization, LiveInferenceRefused
from ghostlab.policy import ceiling_for

from .gate import check

SCHEMA = "ghostlab-release/1"
TARGETS_SCHEMA = "ghostlab-release-targets/1"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
LIVE_STATUSES = ("not_started", "authorized", "passed", "failed")
LOCAL_TARGET, CUSTOMER_TARGET = "local_single_user", "customer_connected"
CUSTOMER_RUNTIME = "authenticated_service"
# Gates 1-4 of docs/acceptance-gates.md. Gate 5 is the live report below.
SECURITY_GATES = ("world_enforcement", "authenticated_isolation", "injection_resistance",
                  "independent_evaluation")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def target_problems(targets: dict) -> list[str]:
    problems = []
    if targets.get("schema") != TARGETS_SCHEMA:
        problems.append("unknown targets schema")
    if (targets.get("release_target") or {}).get("kind") not in (LOCAL_TARGET, CUSTOMER_TARGET):
        problems.append("unknown release target kind")
    rollback = targets.get("rollback_target") or {}
    if not _COMMIT.match(str(rollback.get("commit"))):
        problems.append("no explicit rollback commit")
    if not rollback.get("store_action"):
        problems.append("the rollback target doesn't say what happens to the stores")
    statuses = targets.get("statuses") or {}
    for name in ("runtime", "dependency", "live_capability"):
        if name not in statuses:
            problems.append(f"missing {name} status")
    if (statuses.get("live_capability") or {}).get("status") not in LIVE_STATUSES:
        problems.append("unknown live status")
    return problems


def _evidence_file(root: Path, value) -> Path | None:
    """A readable file inside the release root, or None. A bare string is not evidence."""
    if not isinstance(value, str) or not value.strip():
        return None
    base = root.resolve()
    path = (base / value).resolve()
    return path if path.is_relative_to(base) and path.is_file() else None


def live_evidence_problems(live: dict, root: Path) -> list[str]:
    """Why the live claim isn't proven. Uses the controller's own check of the report."""
    if live.get("status") != "passed":
        return ["live compatibility hasn't passed"]
    bound = [live.get(name) for name in ("provider", "endpoint", "model")]
    if not all(isinstance(value, str) and value for value in bound):
        return ["the live claim doesn't name its provider, endpoint and model"]
    path = _evidence_file(root, live.get("compatibility_report"))
    if path is None:
        return ["the compatibility report isn't a readable file inside the release root"]
    authorization = LiveInferenceAuthorization(
        model=live["model"], max_model_calls=live.get("max_model_calls"),
        compatibility_report=path, confirm_paid_calls=live.get("confirm_paid_calls") is True)
    config = SimpleNamespace(provider=live["provider"], base_url=live["endpoint"],
                             model=live["model"])
    try:
        # A release ships every runnable world, so the report must cover each one's schema.
        for schema in sorted({driver.output_schema for driver in DEFAULT_WORLDS.values()}):
            authorization.check(config, ceiling_for("supervisor"), schema)
    except LiveInferenceRefused as error:
        return [f"compatibility report refused: {error}"]
    except (TypeError, ValueError):  # for example, no spending cap
        return ["compatibility report refused: malformed report or spending cap"]
    if find_secrets(path.read_text("utf-8")):
        return ["compatibility report refused: it isn't sanitized"]
    return []


def customer_connected_blockers(targets: dict, root: Path) -> list[str]:
    """Everything standing between this record and customer-connected use."""
    statuses = targets.get("statuses") or {}
    blockers = []
    if (targets.get("release_target") or {}).get("kind") != CUSTOMER_TARGET:
        blockers.append("this release target is local-only")
    if (statuses.get("runtime") or {}).get("status") != CUSTOMER_RUNTIME:
        blockers.append("the runtime isn't an authenticated service")
    gates = targets.get("security_gates") or {}
    for gate in SECURITY_GATES:
        entry = gates.get(gate) or {}
        if entry.get("status") != "passed" or _evidence_file(root, entry.get("evidence")) is None:
            blockers.append(f"security gate {gate} has no passing evidence")
    blockers.extend(live_evidence_problems(statuses.get("live_capability") or {}, root))
    return blockers


def build_record(*, report: dict, expected: dict, targets: dict, commit: str, root: Path) -> dict:
    blockers = [*check(report, expected), *target_problems(targets)]
    if not _COMMIT.match(commit):
        blockers.append("the candidate must be a full commit id")
    elif commit == (targets.get("rollback_target") or {}).get("commit"):
        blockers.append("the candidate equals its own rollback target")
    statuses = targets.get("statuses") or {}
    live = statuses.get("live_capability") or {}
    live_problems = live_evidence_problems(live, root)
    customer_blockers = customer_connected_blockers(targets, root)
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
        "live_capability": live,  # as claimed; ``live_evidence`` says if it was proven
        "live_evidence": {"validated": not live_problems, "problems": live_problems},
        "customer_connected_use_allowed": not blockers and not customer_blockers,
        "customer_connected_blockers": customer_blockers,
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
