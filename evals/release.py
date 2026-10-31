"""Build a release record: ``ghostlab-release/1``. Evidence for a human, not a deployment.

Runtime, dependency and live-capability statuses stay separate, because passing one says
nothing about the others. Offline evaluations can never move the live status: that takes
a sanitized M01 compatibility report from an authorized live run.

A record is built from an evidence directory, not from a report. The evidence is rescored,
the stored report must say the same, and it must come from the candidate commit on a
clean tree. A dict or a file saying "passed" proves nothing.

A status in the targets file is a claim. ``promotable`` is about offline promotion only.
``customer_connected_use_allowed`` is decided separately and needs all of this:

- a target of kind ``customer_connected``, so a local target can never allow it;
- an authenticated runtime, and every security gate marked passed with an evidence file;
- a documented provider, endpoint and model, accepted by the real ``ProviderConfig``;
- a compatibility report inside the release root that passes the same check the
  controller applies before a paid call, made real calls, and shows the limits AGENTS.md
  requires were probed.

**That last part is unreachable today, on purpose.** ``scripts/probe_provider.py`` lists
timeout, cancel and rate-limit handling under ``not_probed``, reports no budget probe and
writes no ``structured_schemas``, so no report it can write allows customer-connected use.

Nothing here deploys, merges or tags. ``promotable: false`` exits non-zero.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from ghostlab.observability import find_secrets
from ghostlab.orchestration import DEFAULT_WORLDS
from ghostlab.orchestration.inference import LiveInferenceAuthorization, LiveInferenceRefused
from ghostlab.policy import ceiling_for
from ghostlab.providers import Credential, ProviderConfig
from ghostlab.providers.base import ENV, ProviderConfigError

from .gate import check, verified_report

SCHEMA = "ghostlab-release/1"
TARGETS_SCHEMA = "ghostlab-release-targets/1"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
LIVE_STATUSES = ("not_started", "authorized", "passed", "failed")
LOCAL_TARGET, CUSTOMER_TARGET = "local_single_user", "customer_connected"
CUSTOMER_RUNTIME = "authenticated_service"
# Gates 1-4 of docs/acceptance-gates.md. Gate 5 is the live report below.
SECURITY_GATES = ("world_enforcement", "authenticated_isolation", "injection_resistance",
                  "independent_evaluation")
# From AGENTS.md. The access-checked hackathon model is listed only because the user named it.
DOCUMENTED_MODELS = {"meta_responses": {"muse-spark-1.3"},
                     "baseten_chat_completions": {"meta-models/muse-spark-open",
                                                  "meta-models/muse-spark-open-1026"}}
REPORT_VERSION = 1
REQUIRED_PROBES = ("timeout", "cancel", "rate_limit", "budgets")


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
    if not path.is_relative_to(base) or not path.is_file() or path.stat().st_size == 0:
        return None
    return path


def _report_problems(path: Path) -> list[str]:
    """What the controller's check doesn't ask: real calls, and the limits AGENTS.md needs."""
    text = path.read_text("utf-8")
    report = json.loads(text)
    problems = []
    if report.get("report_version") != REPORT_VERSION:
        problems.append("unknown report version")
    for name in ("calls_made", "http_attempts"):
        if type(report.get(name)) is not int or report[name] < 1:
            problems.append(f"{name} shows no real call")
    probed = report.get("probed_limits")
    missing = [name for name in REQUIRED_PROBES
               if not isinstance(probed, list) or name not in probed]
    if missing:
        problems.append(f"not probed: {', '.join(missing)}")
    skipped = report.get("not_probed")
    if not isinstance(skipped, dict):
        problems.append("the report doesn't say what it didn't probe")
    elif listed := [key for key in skipped
                    if any(word in key for word in ("timeout", "cancel", "rate", "budget"))]:
        problems.append(f"listed as not probed: {', '.join(sorted(listed))}")
    if find_secrets(text):
        problems.append("it isn't sanitized")
    return [f"compatibility report refused: {problem}" for problem in problems]


def live_evidence_problems(live: dict, root: Path) -> list[str]:
    """Why the live claim isn't proven. Uses the controller's own check of the report."""
    if live.get("status") != "passed":
        return ["live compatibility hasn't passed"]
    try:
        config = ProviderConfig(provider=live.get("provider"), model=live.get("model"),
                                base_url=live.get("endpoint"),
                                credential=Credential("unused", "release-record"))
    except (ProviderConfigError, TypeError, ValueError, AttributeError):
        return ["the live claim isn't a valid provider, endpoint and model"]
    if config.base_url != ENV[config.provider][3]:
        return ["the live claim names an undocumented endpoint"]
    if config.model not in DOCUMENTED_MODELS[config.provider]:
        return ["the live claim names an undocumented model"]
    path = _evidence_file(root, live.get("compatibility_report"))
    if path is None:
        return ["the compatibility report isn't a readable file inside the release root"]
    authorization = LiveInferenceAuthorization(
        model=live["model"], max_model_calls=live.get("max_model_calls"),
        compatibility_report=path, confirm_paid_calls=live.get("confirm_paid_calls") is True)
    try:
        # A release ships every runnable world, so the report must cover each one's schema.
        for schema in sorted({driver.output_schema for driver in DEFAULT_WORLDS.values()}):
            authorization.check(config, ceiling_for("supervisor"), schema)
    except LiveInferenceRefused as error:
        return [f"compatibility report refused: {error}"]
    except (TypeError, ValueError):  # for example, no spending cap
        return ["compatibility report refused: malformed report or spending cap"]
    return _report_problems(path)


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


def _commit_problems(report: dict, commit: str, repo: Path) -> list[str]:
    if not _COMMIT.match(str(commit)):
        return ["the candidate must be a full commit id"]
    found = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"],
                           capture_output=True, check=False)
    problems = [] if found.returncode == 0 else ["the candidate commit doesn't exist"]
    if report.get("commit") != commit:
        problems.append("the evidence didn't come from the candidate commit")
    if report.get("dirty") is not False:
        problems.append("the evidence came from a tree with local changes")
    return problems


def build_record(*, evidence: Path, expected: dict, targets: dict, commit: str, root: Path,
                 repo: Path | None = None) -> dict:
    report, report_sha256, stored_problems = verified_report(evidence)
    blockers = [*stored_problems, *check(report, expected), *target_problems(targets),
                *_commit_problems(report, commit, repo or root)]
    if commit == (targets.get("rollback_target") or {}).get("commit"):
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
                       "commit": report.get("commit"),
                       "cases": (report.get("aggregate") or {}).get("cases"),
                       "report_sha256": report_sha256},
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
    parser.add_argument("--evidence", type=Path, required=True,
                        help="The directory evals.run wrote")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--expected", type=Path, default=Path("evals/expected.json"))
    parser.add_argument("--targets", type=Path, default=Path("deploy/release-targets.json"))
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    record = build_record(evidence=args.evidence,
                          expected=json.loads(args.expected.read_text("utf-8")),
                          targets=json.loads(args.targets.read_text("utf-8")),
                          commit=args.commit, root=args.root)
    print(json.dumps(record, indent=2))
    return 0 if record["promotable"] else 1


if __name__ == "__main__":
    sys.exit(main())
