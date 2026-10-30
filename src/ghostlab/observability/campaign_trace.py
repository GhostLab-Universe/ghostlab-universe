"""Rebuild a campaign's trace from its stored state, without instrumenting the controller.

M09 doesn't edit other packages, so until M05 writes telemetry directly, this turns what
a finished or paused campaign already recorded into ``ghostlab-telemetry/1`` records. It
reads plain JSON only (``controller.state()``), so it stays dependency-free.

Only structured fields are read. Event summaries and error messages are skipped on
purpose: they can quote model output, and telemetry has no free text.

Times are what the state recorded. Tasks, errors and HTTP attempts have no timestamp of
their own, so they carry the campaign's last recorded time and their ``latency_ms`` is
unknown. Real per-call timing needs M05 and M01 to write spans as they run.
"""

import hashlib
from collections.abc import Iterable, Mapping

from .schema import TraceContext, Usage, Versions
from .telemetry import Telemetry

_EVENT_NAMES = {"approval_requested": "approval.requested", "approval_decided": "approval.decided",
                "budget_exhausted": "budget.exhausted", "finding_recorded": "finding.recorded",
                "run_completed": "run.completed", "task_status_changed": "task.status"}
_ERROR_NAMES = {"validation_failed": "validation.failed", "budget_exhausted": "budget.exhausted",
                "provider_invalid_output": "validation.failed", "cancelled": "cancellation",
                "forbidden": "policy.denied"}
_VALIDATION_CODES = {"validation_failed", "provider_invalid_output"}
ROOT_SPAN = "campaign.run"


def _derived_span_id(trace_id: str, label: str) -> str:
    """Stable, so rebuilding the same campaign gives the same ids."""
    return hashlib.sha256(f"{trace_id}\x1f{label}".encode()).hexdigest()[:16]


def _approval_ref(event: Mapping) -> str | None:
    return next((ref["id"] for ref in event.get("refs", ()) if ref.get("kind") == "approval"),
                None)


def emit_campaign_trace(telemetry: Telemetry, state: Mapping, *,
                        versions: Versions = Versions(), usage: Usage | None = None,
                        attempts: Iterable[Mapping] = (),
                        approval_status: str | None = None) -> str:
    """Write one campaign's trace. Returns the root span id.

    ``usage`` is the campaign total, and unknown stays unknown when it's omitted.
    ``attempts`` lists HTTP attempts as ``{"http_status": int | None, "outcome": str}``.
    """
    trace_id = state["trace_id"]
    provider = state.get("provider") or {}
    versions = Versions(role=versions.role, provider=provider.get("provider"),
                        model=provider.get("model"), prompt_version=versions.prompt_version,
                        skill_id=versions.skill_id, skill_version=versions.skill_version,
                        workflow_version=versions.workflow_version,
                        world_version=(state.get("spec") or {}).get("world_version"))
    root_id = _derived_span_id(trace_id, "root")
    context = TraceContext(trace_id=trace_id, project_id=state.get("project_id"),
                           campaign_id=state.get("campaign_id"), span_id=root_id,
                           versions=versions)
    events, attempts = list(state.get("events", ())), list(attempts)
    started = state["started_at"]
    ended = events[-1]["occurred_at"] if events else started
    errors = list(state.get("errors", ()))

    for event in events:
        name = _EVENT_NAMES.get(event["kind"], "campaign.status")
        telemetry.event(name, context.with_task(event.get("task_id")), at=event["occurred_at"],
                        approval_id=_approval_ref(event),
                        attributes={"sequence": event["sequence"],
                                    "run_id": event.get("run_id")})
    for task in state.get("tasks", ()):
        telemetry.event("task.status", context.with_task(task["task_id"], role=task["role"]),
                        at=ended, outcome=task["status"], attempt=task["attempt"])
    for error in errors:
        telemetry.event(_ERROR_NAMES.get(error["code"], "campaign.error"), context, at=ended,
                        error_code=error["code"],
                        attributes={"side_effect": error.get("side_effect")})
    for index, attempt in enumerate(attempts, 1):
        telemetry.event("model.attempt", context, at=ended, attempt=index,
                        outcome=attempt.get("outcome"),
                        attributes={"http_status": attempt.get("http_status")})

    approval = state.get("approval") or {}
    root = TraceContext(trace_id=trace_id, project_id=context.project_id,
                        campaign_id=context.campaign_id, versions=versions)
    telemetry.finished_span(
        ROOT_SPAN, root, span_id=root_id, started_at=started, ended_at=ended,
        outcome=state.get("outcome") or state.get("status"), usage=usage,
        retries=max(0, len(attempts) - 1) if attempts else None,
        cancelled=state.get("status") == "cancelled",
        validation_failures=sum(error["code"] in _VALIDATION_CODES for error in errors),
        approval_id=approval.get("approval_id"), approval_status=approval_status,
        attributes={"iteration": state.get("iteration"), "event_count": len(events),
                    "finding_count": len(state.get("findings", ()))})
    return root_id
