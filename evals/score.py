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
from .oracles import (expected_citations, expected_verdicts, fault_evidence, journey_oracle,
                      refund_oracle, tool_counts)

SCHEMA = "ghostlab-evaluation/1"
RUN_IDENTITY = "run.json"  # the commit the evidence came from, written by evals.run
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
        results[name] = {**_run_oracle(campaign["world"], campaign["fixture"], run["body"]),
                         "tools": tool_counts(run["body"])}
    return results, broken


def _verdicts_match(campaign: dict) -> bool:
    """Every invariant, recomputed here, must match the stored checks and the findings.

    A fork or repaired run must have a finding for every invariant: none is a failure,
    not an agreement. A baseline run has no findings, so only its checks are compared.
    """
    for name, run in campaign["runs"].items():
        expected = expected_verdicts(campaign["world"], campaign["fixture"], run["body"])
        if run["body"]["checks"] != expected:
            return False
        if name.endswith("/baseline"):
            continue
        run_id = run["artifact_id"].split(".", 1)[1]
        found = {item["invariant"]: item["verdict"] == "passed"
                 for item in campaign["findings"] if item["run_id"] == run_id}
        if found != expected:
            return False
    return True


def _citations_match(campaign: dict) -> bool:
    """Each failed finding must cite exactly the events a recomputation points to."""
    for name, run in campaign["runs"].items():
        if name.endswith("/baseline"):
            continue
        run_id = run["artifact_id"].split(".", 1)[1]
        cited = {item["invariant"]: item["evidence_event_ids"] for item in campaign["findings"]
                 if item["run_id"] == run_id and item["verdict"] == "failed"}
        if cited != expected_citations(campaign["world"], campaign["fixture"], run["body"]):
            return False
    return True


def _first_difference(baseline: dict, fork: dict) -> int | None:
    for index in range(max(len(baseline["trace"]), len(fork["trace"]))):
        left = baseline["trace"][index] if index < len(baseline["trace"]) else None
        right = fork["trace"][index] if index < len(fork["trace"]) else None
        if left != right:
            return index + 1
    return None


def _diffs_match(campaign: dict) -> bool:
    """The Reality Diff shown to the operator must say what the stored runs say."""
    for experiment_id, diff in campaign["diffs"].items():
        baseline, fork = (campaign["runs"][f"{experiment_id}/{name}"]["body"]
                          for name in ("baseline", "fork"))
        expected = expected_verdicts(campaign["world"], campaign["fixture"], fork)
        if sorted(diff["violated_invariants"]) != sorted(
                name for name, passed in expected.items() if not passed):
            return False
        if diff["first_divergence_event"] != _first_difference(baseline, fork):
            return False
        if campaign["world"] == REFUND and (
                diff["baseline_refunded_minor"] != baseline["final_state"]["total_refunded_minor"]
                or diff["faulted_refunded_minor"] != fork["final_state"]["total_refunded_minor"]):
            return False
    return True


def _approval_events_match(campaign: dict) -> bool:
    """Events are numbered in order, and a human's decision is always on the record."""
    kinds = [event["kind"] for event in campaign["events"]]
    if [event["sequence"] for event in campaign["events"]] != list(range(1, len(kinds) + 1)):
        return False
    approval = campaign["approval"]
    if approval is None:
        return "approval_requested" not in kinds
    decided = approval["status"] in {"approved", "rejected", "consumed"}
    return "approval_requested" in kinds and ("approval_decided" in kinds) == decided


def _runs_are_consistent(campaign: dict, oracles: dict) -> bool:
    """Each stored run hangs together, whatever its verdicts say."""
    for oracle in oracles.values():
        if not oracle["trace_contiguous"] or not oracle["final_state_matches_trace"]:
            return False
        if oracle.get("wrong_customer_or_order") or oracle.get("mutations_outside_issue_refund"):
            return False
        if oracle.get("mutated_on_denial"):
            return False
    return True


def _tool_ledger_matches(campaign: dict) -> bool:
    """The ledger charges exactly the tool calls in the runs that were kept."""
    charged = sum(len(run["body"]["trace"]) for run in campaign["runs"].values())
    return campaign["ledger"]["tool_calls"] == {"committed": charged, "unknown": 0}


def _coverage_matches(campaign: dict) -> bool:
    """Each experiment's recorded coverage must say what its stored runs say."""
    for experiment_id, coverage in campaign["coverage"].items():
        baseline, fork = (campaign["runs"][f"{experiment_id}/{name}"]["body"]
                          for name in ("baseline", "fork"))
        triggered = coverage["faults_triggered"]
        finished = baseline["status"] == fork["status"] == "completed"
        if coverage != {"baseline": baseline["status"], "fork": fork["status"],
                        "faults_triggered": triggered, "complete": triggered and finished}:
            return False
        seen = fault_evidence(campaign["world"], fork)
        if triggered and not seen:
            return False
        # A refund fault is exact. A journey fault may be seen yet change nothing, so only
        # a delivery that broke permission proves that it fired.
        if campaign["world"] == REFUND and seen and not triggered:
            return False
        if campaign["world"] != REFUND and not triggered and (
                journey_oracle(fork)["unpermitted_deliveries"]):
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
        campaign = evidence["campaigns"][name]
        wanted = (len(campaign["events"]) + len(campaign["tasks"]) + len(campaign["errors"])
                  + len(evidence["http_attempts"][name]) + 1)  # the root span
        if len(stored) != wanted:
            problems.append(f"{name}: {len(stored)} records, the campaign needs {wanted}")
        problems.extend(f"{name}: {item}" for item in found)
        roots = [r for r in stored if r["name"] == "campaign.run" and r["parent_span_id"] is None]
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
                             "approval": (c["approval"] or {}).get("status"),
                             "findings": sorted([item["run_id"].rsplit("-", 1)[-1],
                                                 item["invariant"], item["verdict"],
                                                 item["evidence_event_ids"]]
                                                for item in c["findings"]),
                             "events": [event["kind"] for event in c["events"]],
                             "diffs": list(c["diffs"].values()),
                             "coverage": sorted(c["coverage"].values(),
                                                key=lambda item: json.dumps(item, sort_keys=True)),
                             "memory": c["memory"],
                             "ledger": {name: c["ledger"][name]
                                        for name in ("model_calls", "tool_calls")}}
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
        "every_invariant_verdict_matches_a_recomputation": all(
            _verdicts_match(c) for c in evidence["campaigns"].values()),
        "cited_events_match_a_recomputation": all(
            _citations_match(c) for c in evidence["campaigns"].values()),
        "reality_diff_matches_the_stored_runs": all(
            _diffs_match(c) for c in evidence["campaigns"].values()),
        "approval_events_match_the_decisions": all(
            _approval_events_match(c) for c in evidence["campaigns"].values()),
        "stored_runs_are_internally_consistent": all(
            _runs_are_consistent(c, oracles[name])
            for name, c in evidence["campaigns"].items()),
        "tool_ledger_matches_the_stored_runs": all(
            _tool_ledger_matches(c) for c in evidence["campaigns"].values()),
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
    summary = aggregate(scored)
    summary["cases"] = {"total": len(results),  # a case with no evidence still counts
                        "passed": sum(case["status"] == "passed" for case in results)}
    identity = directory / RUN_IDENTITY
    bound = json.loads(identity.read_text("utf-8")) if identity.exists() else {}
    return {"schema": SCHEMA, "inference": "mocked", "live_provider_contacted": False,
            "commit": bound.get("commit"), "dirty": bound.get("dirty"),
            "package_path": bound.get("package_path"), "code_sha256": bound.get("code_sha256"),
            "origin_verified": False,  # nothing is signed; see evals/identity.py
            "fixture_label": FIXTURE_LABEL,
            "environment": {"python": platform.python_version(), "platform": sys.platform},
            "aggregate": summary, "cases": results, "deferred": DEFERRED}
