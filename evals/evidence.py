"""Copy what a campaign recorded into one evidence file. Nothing is judged here.

Scoring (``evals.score``) reads only these files, so a result can be recomputed later
and a changed artifact changes the result. Free text (event summaries, error messages,
hypotheses) is left out: it can quote model output, and no score depends on it.
"""

import hashlib
import json
from pathlib import Path

from ghostlab.observability import FileSink, Telemetry, Usage, Versions, emit_campaign_trace
from ghostlab.orchestration import DEFAULT_WORLDS, ORCHESTRATION_VERSION, ledger_key
from ghostlab.policy import root_scope
from ghostlab.security.redaction import Redactor

from .harness import OPERATOR, PROJECT, SECRET, SECRETS, Stack

RESOURCES = ("model_calls", "tool_calls", "input_tokens", "output_tokens")


def canonical_sha256(value) -> str:
    """The digest the controller stores a run under: canonical JSON, not pretty-printed."""
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _read_run(stack: Stack, ref: dict) -> dict:
    view = stack.artifacts.for_project(root_scope(PROJECT, OPERATOR))
    stored = view.ref(ref["id"])
    data = view.read(stored)  # M04 checks size and digest on read
    return {"artifact_id": stored.artifact_id, "sha256": stored.sha256,
            "body": json.loads(data)}


def _runs(stack: Stack, state: dict) -> dict:
    runs = {}
    for experiment_id, experiment in state.get("experiments", {}).items():
        runs[f"{experiment_id}/baseline"] = _read_run(stack, experiment["baseline"])
        runs[f"{experiment_id}/fork"] = _read_run(stack, experiment["fork"])
    repair = state.get("repair") or {}
    if repair.get("run"):
        runs["repaired"] = _read_run(stack, repair["run"])
    return runs


def _ledger(stack: Stack, state: dict) -> dict:
    key = ledger_key(state["project_id"], state["campaign_id"])
    return {resource: dict(zip(("committed", "unknown"), stack.ledger.committed(key, resource)))
            for resource in RESOURCES}


def _approval(stack: Stack, state: dict) -> dict | None:
    approval = state.get("approval")
    if not approval:
        return None
    stored = stack.approval_store.get(PROJECT, approval["approval_id"])
    return {"approval_id": stored.approval_id, "status": stored.status,
            "action_digest": stored.action_digest}


def _memory(stack: Stack, state: dict) -> dict:
    """How many records this campaign stored, by status, read back from the store."""
    scope = root_scope(PROJECT, OPERATOR)
    statuses = [stack.memory_store.get(scope, record_id).status
                for record_id in state["memory_ids"]]
    return {status: statuses.count(status) for status in sorted(set(statuses))}


def campaign_evidence(stack: Stack, campaign_id: str) -> dict:
    """Everything the campaign left behind, read back through the real stores."""
    with stack.controller() as controller:
        state = controller.state(campaign_id)
    return {
        "campaign_id": campaign_id, "world": state["spec"]["world_version"],
        "trace_id": state["trace_id"], "fixture": state.get("fixture"),
        "status": state["status"], "outcome": state["outcome"], "paused": state["paused"],
        "iteration": state["iteration"],
        "errors": [{key: error[key] for key in ("code", "retryable", "side_effect")}
                   for error in state["errors"]],
        "tasks": [{key: task[key] for key in ("task_id", "kind", "role", "status", "attempt",
                                              "depends_on")} for task in state["tasks"]],
        "findings": [{key: finding.get(key) for key in ("run_id", "invariant", "verdict",
                                                        "evidence_event_ids")}
                     for finding in state["findings"]],
        "events": [{key: event.get(key) for key in ("sequence", "kind", "task_id", "run_id")}
                   for event in state["events"]],
        "repair": {key: value for key, value in (state.get("repair") or {}).items()
                   if key != "run"} or None,
        "approval": _approval(stack, state),
        "coverage": {experiment_id: experiment.get("coverage")
                     for experiment_id, experiment in state.get("experiments", {}).items()},
        "runs": _runs(stack, state), "ledger": _ledger(stack, state),
        "memory_ids": len(state["memory_ids"]),
        "memory": _memory(stack, state)}


def _usage(evidence: dict) -> Usage:
    """Token totals from the ledger. A reservation with unknown actuals makes them unknown."""
    def known(resource):
        row = evidence["ledger"][resource]
        return None if row["unknown"] else row["committed"]
    return Usage(input_tokens=known("input_tokens"), output_tokens=known("output_tokens"))


def write_trace(stack: Stack, campaign_id: str, evidence: dict, path: Path) -> None:
    """A ``ghostlab-telemetry/1`` trace for one campaign, into a size-bounded local file."""
    with stack.controller() as controller:
        state = controller.state(campaign_id)
    driver = DEFAULT_WORLDS[evidence["world"]]
    skill = stack.services().skills.get(driver.skill_id)
    versions = Versions(role="supervisor", skill_id=skill.spec.skill_id,
                        skill_version=str(skill.spec.version),
                        prompt_version=skill.spec.prompt_version,
                        workflow_version=ORCHESTRATION_VERSION)
    telemetry = Telemetry(FileSink(path), redactor=Redactor([SECRET]), clock=stack.clock,
                          strict=True)
    emit_campaign_trace(telemetry, state, versions=versions, usage=_usage(evidence),
                        attempts=stack.transport.attempts,
                        approval_status=(evidence["approval"] or {}).get("status"))
    telemetry.close()


def scan_stores(root: Path) -> dict:
    """Search every byte of a local store for the fixture credential and synthetic secret.

    Reports the file and which secret by position, never the secret itself.
    """
    scanned, hits = 0, []
    for path in sorted(item for item in Path(root).rglob("*") if item.is_file()):
        scanned += 1
        data = path.read_bytes()
        hits.extend(f"{path.name}:secret[{index}]" for index, secret in enumerate(SECRETS)
                    if secret.encode() in data)
    return {"files_scanned": scanned, "files_with_secret": hits}
