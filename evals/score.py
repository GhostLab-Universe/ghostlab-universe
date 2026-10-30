"""Score stored evidence: ``ghostlab-evaluation/1``. Reads files only.

Nothing here can reach a controller, a store or a model, so a report is a pure function
of the evidence directory. Change an artifact and the result changes.

A rate is always reported with its counts. Unknown stays null: mocked inference gives no
provider latency and no cost, so neither is estimated. Local wall time is reported under
its own label because it's the time of this machine running fixtures.
"""

import json
import platform
import sys
from pathlib import Path

from ghostlab.observability import find_secrets, read_records

from .cases import CASES
from .evidence import canonical_sha256
from .harness import FIXTURE_LABEL, REFUND, SECRETS
from .oracles import agrees_with_controller, journey_oracle, refund_oracle, tool_counts

SCHEMA = "ghostlab-evaluation/1"
DEFERRED = [
    {"item": "random vs one-pass vs adaptive exploration",
     "reason": "Inference is scripted, so there's nothing to sample. A fair comparison needs "
               "equal budgets, a real proposal source and reported sample sizes. No result "
               "is claimed."},
    {"item": "provider latency", "reason": "No provider was contacted."},
    {"item": "cost per successful task",
     "reason": "No price is verified and the ledger records no cost, so cost is unknown."},
    {"item": "full-application acceptance",
     "reason": "Needs M07 (API) and M08 (interface). Placeholders are skipped, not faked."},
    {"item": "service-relay/1", "reason": "Specified, not runnable."},
]


def _run_oracle(world: str, fixture: dict | None, body: dict) -> dict:
    if world == REFUND:
        return refund_oracle(fixture, body)
    return journey_oracle(body)


def _oracles(campaign: dict) -> tuple[dict, list[str]]:
    """Oracle results per run, and any run whose stored digest doesn't match its body."""
    results, broken = {}, []
    for name, run in campaign["runs"].items():
        if canonical_sha256(run["body"]) != run["sha256"]:
            broken.append(name)
        short = name.rsplit("/", 1)[-1]
        results[short] = {**_run_oracle(campaign["world"], campaign["fixture"], run["body"]),
                          "tools": tool_counts(run["body"])}
    return results, broken


def _agreement(campaign: dict) -> bool:
    """The controller's findings for each run must match the independent oracle."""
    for name, run in campaign["runs"].items():
        run_id = run["artifact_id"].split(".", 1)[1]
        findings = [item for item in campaign["findings"] if item["run_id"] == run_id]
        if findings and not agrees_with_controller(
                campaign["world"], _run_oracle(campaign["world"], campaign["fixture"],
                                               run["body"]), findings):
            return False
    return True


def _coverage_matches(campaign: dict) -> bool:
    """Each experiment's recorded coverage must say what its stored runs say."""
    for experiment_id, coverage in campaign["coverage"].items():
        statuses = [campaign["runs"][f"{experiment_id}/{name}"]["body"]["status"]
                    for name in ("baseline", "fork")]
        if coverage != {"baseline": statuses[0], "fork": statuses[1],
                        "complete": statuses == ["completed", "completed"]}:
            return False
    return True


def _coverage_matches(campaign: dict) -> bool:
    """Each experiment's recorded coverage must say what its stored runs say."""
    for experiment_id, coverage in campaign["coverage"].items():
        statuses = [campaign["runs"][f"{experiment_id}/{name}"]["body"]["status"]
                    for name in ("baseline", "fork")]
        if coverage != {"baseline": statuses[0], "fork": statuses[1],
                        "complete": statuses == ["completed", "completed"]}:
            return False
    return True


def _trace_problems(telemetry_dir: Path, case_id: str, evidence: dict) -> dict:
    files = {name: telemetry_dir / f"{case_id}.{name}.jsonl" for name in evidence["campaigns"]}
    records, problems, leaks = 0, [], []
    for name, path in files.items():
        if not path.exists():
            problems.append(f"{name}: no trace")
            continue
        stored, found = read_records(path)
        records += len(stored)
        problems.extend(f"{name}: {item}" for item in found)
        roots = [r for r in stored if r["name"] == "campaign.run" and r["parent_span_id"] is None]
        campaign = evidence["campaigns"][name]
        if len(roots) != 1 or roots[0]["trace_id"] != campaign["trace_id"]:
            problems.append(f"{name}: root span missing or from another trace")
        if {r["trace_id"] for r in stored} != {campaign["trace_id"]}:
            problems.append(f"{name}: records from another trace")
        leaks.extend(find_secrets(path.read_text("utf-8"), SECRETS))
    return {"records": records, "problems": problems, "leaks": sorted(set(leaks))}


def _handoffs(tasks: list[dict]) -> dict:
    """A handoff is a task that depends on another. It fails if the work never arrived."""
    handoffs = [task for task in tasks if task["depends_on"]]
    failed = [task for task in handoffs if task["status"] in {"skipped", "failed", "cancelled"}]
    return {"total": len(handoffs), "failed": len(failed)}


def _ledger_sum(campaigns: list[dict], resource: str) -> dict:
    return {"known_sum": sum(c["ledger"][resource]["committed"] for c in campaigns
                             if not c["ledger"][resource]["unknown"]),
            "unknown_count": sum(c["ledger"][resource]["unknown"] for c in campaigns)}


def case_metrics(evidence: dict, oracles: dict) -> dict:
    campaigns = list(evidence["campaigns"].values())
    tools = [run["tools"] for runs in oracles.values() for run in runs.values()]
    tool_total = {key: sum(item[key] for item in tools)
                  for key in ("total", "succeeded", "failed", "failed_injected_fault",
                              "failed_guard_denied")}
    handoffs = [_handoffs(c["tasks"]) for c in campaigns]
    tasks = [task for c in campaigns for task in c["tasks"]]
    approvals = [c["approval"]["status"] for c in campaigns if c["approval"]]
    decided = [status for status in approvals if status != "pending"]
    attempts = sum(len(items) for items in evidence["http_attempts"].values())
    return {
        "tool_calls": tool_total,
        "handoffs": {key: sum(item[key] for item in handoffs) for key in ("total", "failed")},
        "recovery": evidence["recovery"],
        "human_decisions": {"decided": len(decided),
                            "rejected": decided.count("rejected"), "pending": len(approvals)
                            - len(decided)},
        "tasks": {"total": len(tasks),
                  "succeeded": sum(task["status"] == "succeeded" for task in tasks)},
        "successful_campaigns": sum(c["outcome"] == "repair_verified" for c in campaigns),
        "model_attempts": attempts,
        "fixture_tokens": {"input": _ledger_sum(campaigns, "input_tokens"),
                           "output": _ledger_sum(campaigns, "output_tokens")},
        "cost_minor": None, "cost_per_successful_task": None, "provider_latency_ms": None}


def _fingerprint(evidence: dict, metrics: dict) -> dict:
    """Everything deterministic. Local wall time stays out: it isn't a behaviour."""
    return {
        "campaigns": {name: {"status": c["status"], "outcome": c["outcome"],
                             "paused": c["paused"],
                             "tasks": [[t["kind"], t["status"]] for t in c["tasks"]],
                             "errors": [e["code"] for e in c["errors"]],
                             "runs": {key: run["sha256"] for key, run in c["runs"].items()},
                             "approval": (c["approval"] or {}).get("status")}
                      for name, c in evidence["campaigns"].items()},
        "observations": evidence["observations"],
        "http": {name: [item["http_status"] for item in items]
                 for name, items in evidence["http_attempts"].items()},
        "metrics": {key: value for key, value in metrics.items()}}


def score_case(case, evidence: dict, telemetry_dir: Path) -> dict:
    oracles, broken = {}, []
    for name, campaign in evidence["campaigns"].items():
        oracles[name], damaged = _oracles(campaign)
        broken.extend(f"{name}/{item}" for item in damaged)
    metrics = case_metrics(evidence, oracles)
    trace = _trace_problems(telemetry_dir, case.case_id, evidence)
    text = json.dumps(evidence)
    expectations = {
        "evidence_digests_match": broken == [],
        "findings_match_the_independent_oracle": all(
            _agreement(c) for c in evidence["campaigns"].values()),
        "coverage_matches_the_stored_runs": all(
            _coverage_matches(c) for c in evidence["campaigns"].values()),
        "no_secret_in_stores": evidence["store_scan"]["files_with_secret"] == []
        and evidence["store_scan"]["files_scanned"] > 0,
        "no_secret_in_evidence": find_secrets(text, SECRETS) == [],
        "trace_valid_and_complete": trace["problems"] == [],
        "no_secret_in_telemetry": trace["leaks"] == []}
    try:
        expectations.update(case.expect({**evidence, "oracles": oracles}))
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        expectations[f"evidence_shape:{type(error).__name__}"] = False
    failed = sorted(name for name, passed in expectations.items() if not passed)
    return {"case_id": case.case_id, "category": case.category, "world": case.world,
            "status": "passed" if not failed else "failed", "failed_expectations": failed,
            "expectations": {name: bool(value) for name, value in sorted(expectations.items())},
            "metrics": metrics, "telemetry_records": trace["records"],
            "local_wall_ms": evidence["local_wall_ms"],
            "fingerprint": _fingerprint(evidence, metrics)}


def _sum(cases: list[dict], *path) -> int:
    total = 0
    for case in cases:
        value = case["metrics"]
        for key in path:
            value = value[key]
        total += value
    return total


def _rate(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator,
            "rate": round(numerator / denominator, 4) if denominator else None}


def aggregate(cases: list[dict]) -> dict:
    return {**_summary(cases), "by_category": {
        category: _summary([case for case in cases if case["category"] == category])
        for category in sorted({case["category"] for case in cases})}}


def _summary(cases: list[dict]) -> dict:
    tools = _sum(cases, "tool_calls", "total")
    return {
        "cases": {"total": len(cases),
                  "passed": sum(case["status"] == "passed" for case in cases)},
        "tool_success": {**_rate(_sum(cases, "tool_calls", "succeeded"), tools),
                         "failed_injected_fault": _sum(cases, "tool_calls",
                                                       "failed_injected_fault"),
                         "failed_guard_denied": _sum(cases, "tool_calls", "failed_guard_denied")},
        "handoff_failures": _rate(_sum(cases, "handoffs", "failed"),
                                  _sum(cases, "handoffs", "total")),
        "recovery": _rate(_sum(cases, "recovery", "succeeded"),
                          _sum(cases, "recovery", "attempted")),
        "human_decisions": {"decided": _sum(cases, "human_decisions", "decided"),
                            "rejected": _sum(cases, "human_decisions", "rejected"),
                            "pending": _sum(cases, "human_decisions", "pending")},
        "successful_tasks": _sum(cases, "tasks", "succeeded"),
        "successful_campaigns": _sum(cases, "successful_campaigns"),
        "model_attempts": _sum(cases, "model_attempts"),
        "latency": {"provider_ms": None,
                    "local_wall_ms_total": round(sum(c["local_wall_ms"] for c in cases), 3),
                    "note": "Local wall time with mocked inference, not provider latency"},
        "cost": {"total_minor": None, "per_successful_task": None,
                 "note": "Unknown: no verified price, and unknown is not zero"}}


def score_directory(directory: Path) -> dict:
    """Build a report from ``<directory>/evidence`` and ``<directory>/telemetry``."""
    directory = Path(directory)
    results = []
    for case in CASES:
        path = directory / "evidence" / f"{case.case_id}.json"
        if not path.exists():
            results.append({"case_id": case.case_id, "category": case.category,
                            "world": case.world, "status": "failed",
                            "failed_expectations": ["evidence_missing"], "expectations": {},
                            "metrics": None, "telemetry_records": 0, "local_wall_ms": 0,
                            "fingerprint": None})
            continue
        results.append(score_case(case, json.loads(path.read_text("utf-8")),
                                  directory / "telemetry"))
    scored = [case for case in results if case["metrics"] is not None]
    return {"schema": SCHEMA, "inference": "mocked", "live_provider_contacted": False,
            "fixture_label": FIXTURE_LABEL,
            "environment": {"python": platform.python_version(), "platform": sys.platform},
            "aggregate": aggregate(scored), "cases": results, "deferred": DEFERRED}
