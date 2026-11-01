"""UI-neutral evidence for external target runs (P3): three distinct outcomes.

:func:`run_external_attempt` runs one plain-Python agent against a reset
environment through the controlled gateway and records either
``agent_retest`` (the agent ran fresh; the verdict is ``checks_passed``)
or ``boundary_enforcement`` (the gateway denied a proposal; the verdict is
the denial, and the denied call never touched the world).
:func:`replay_recorded` re-executes recorded requests with no agent and
records ``recorded_replay`` (the verdict is ``matches_recording``). The
three outcomes share no pass/fail badge: only a completed retest carries
``checks_passed``. Every record names its Explorer, Target and Verifier
identities in separate sections. Standard library only.
"""

from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

from ghostlab.extensions.environment import EnvironmentAdapter
from ghostlab.extensions.registry import check_environment
from ghostlab.extensions.runner import first_divergence, replay_trace, run_attempt
from ghostlab.extensions.target import TargetTask

from .external import (ControlledGateway, ExternalTargetAdapter,
                       check_external_target)

RECORDED_REPLAY = "recorded_replay"
AGENT_RETEST = "agent_retest"
BOUNDARY_ENFORCEMENT = "boundary_enforcement"

OUTCOME_KINDS = (RECORDED_REPLAY, AGENT_RETEST, BOUNDARY_ENFORCEMENT)


def _text(value: Any, label: str, *, max_length: int = 200) -> str:
    if type(value) is not str or not value or len(value) > max_length:
        raise ValueError(f"Invalid {label}")
    return value


def _explorer_record(explorer: Mapping[str, Any]) -> dict[str, str]:
    """The Explorer identity as a validated plain-data copy, kept separate."""
    if not isinstance(explorer, Mapping):
        raise ValueError("Invalid explorer identity")
    record = {}
    for key in ("id", "version"):
        value = explorer.get(key)
        _text(value, f"explorer {key}")
        record[key] = value
    return record


def _check_dicts(env: EnvironmentAdapter, trace: list, state: Mapping[str, Any]) -> list:
    """Deterministic invariant checks as plain data, from evidence only."""
    found = []
    for check in env.evaluate(trace=trace, state=state):
        found.append({"name": check.name, "passed": check.passed,
                      "evidence_event_ids": list(check.evidence_event_ids),
                      "observed": check.observed, "limit": check.limit,
                      "unit": check.unit})
    return found


def _target_record(adapter: ExternalTargetAdapter) -> dict[str, Any]:
    """The Target identity plus its visible actions, kept separate."""
    info = adapter.info()
    return {"adapter_id": info.adapter_id, "adapter_version": info.adapter_version,
            "model": info.model, "provider": info.provider, "endpoint": info.endpoint,
            "actions": [{"tool": action["tool"],
                         "arguments": dict(action["arguments"]),
                         "status": action["status"]}
                        for action in info.actions],
            "close_reason": adapter.close_reason}


def run_external_attempt(env: EnvironmentAdapter, agent, task: TargetTask, *,
                         gateway: ControlledGateway, cap: int,
                         explorer: Mapping[str, Any], verifier_version: str,
                         scenario: Mapping[str, Any] | None = None,
                         should_stop: Callable[[], str | None] | None = None,
                         cooperative_stop: bool = True) -> dict[str, Any]:
    """Run one external agent against a reset environment; return its record.

    ``env`` must already be reset to the recorded fixture, scenario and
    actor. Every proposal crosses ``gateway`` before dispatch; a denial
    ends the attempt as ``boundary_enforcement`` without touching the
    world, otherwise the record is an ``agent_retest``. Only a completed,
    unstopped retest carries ``checks_passed``; denied, stopped and
    over-budget runs carry no pass/fail badge.
    """
    check_environment(env)
    check_external_target(agent)
    if not isinstance(task, TargetTask):
        raise TypeError("run_external_attempt task must be a TargetTask")
    if not isinstance(gateway, ControlledGateway):
        raise TypeError("run_external_attempt gateway must be a ControlledGateway")
    explorer_record = _explorer_record(explorer)
    _text(verifier_version, "verifier version")
    if scenario is not None and not isinstance(scenario, Mapping):
        raise ValueError("Invalid scenario")
    adapter = ExternalTargetAdapter(agent)
    result = run_attempt(env, adapter, task, authorize=gateway.authorize, cap=cap,
                         should_stop=should_stop, cooperative_stop=cooperative_stop)
    record: dict[str, Any] = {
        "explorer": explorer_record,
        "target": _target_record(adapter),
        "verifier": {"version": verifier_version},
        "status": result.status,
        "tool_calls": result.tool_calls,
        "trace": deepcopy(result.trace),
        "state": deepcopy(dict(result.state)),
        "checks": _check_dicts(env, result.trace, result.state),
        "stopped": result.stopped,
    }
    if scenario is not None:
        record["faults_fired"] = sorted(env.faults_fired(scenario, result.trace))
    if result.denial is not None:
        record["outcome_kind"] = BOUNDARY_ENFORCEMENT
        record["denial"] = {"code": getattr(result.denial, "code", "denied"),
                            "reason": getattr(result.denial, "reason", str(result.denial))}
        record["world_touched_by_denied_call"] = False
        return record
    record["outcome_kind"] = AGENT_RETEST
    record["denial"] = None
    if result.stopped is None and result.status == "completed":
        record["checks_passed"] = all(check["passed"] for check in record["checks"])
    return record


def replay_recorded(env: EnvironmentAdapter, recorded_trace: list, *,
                    explorer: Mapping[str, Any], verifier_version: str,
                    source: Mapping[str, Any]) -> dict[str, Any]:
    """Re-execute recorded requests against a reset environment; no agent runs.

    ``env`` must already be reset to the recorded fixture, scenario and
    actor. ``source`` names the replayed record (``outcome_kind``,
    ``adapter_id``, ``adapter_version``, optional model/provider/endpoint).
    The record is a ``recorded_replay``: its verdict is ``matches_recording``,
    never a pass/fail badge, and ``target.agent_reran`` is always False.
    """
    check_environment(env)
    if type(recorded_trace) is not list:
        raise ValueError("Invalid recorded trace")
    explorer_record = _explorer_record(explorer)
    _text(verifier_version, "verifier version")
    if not isinstance(source, Mapping):
        raise ValueError("Invalid replay source")
    for key in ("outcome_kind", "adapter_id", "adapter_version"):
        _text(source.get(key), f"replay source {key}")
    for key in ("model", "provider", "endpoint"):
        value = source.get(key)
        if value is not None:
            _text(value, f"replay source {key}", max_length=500)
    observed = replay_trace(env, recorded_trace)
    divergence = first_divergence(recorded_trace, observed.trace)
    return {
        "outcome_kind": RECORDED_REPLAY,
        "explorer": explorer_record,
        "target": {"adapter_id": source["adapter_id"],
                   "adapter_version": source["adapter_version"],
                   "model": source.get("model"), "provider": source.get("provider"),
                   "endpoint": source.get("endpoint"), "agent_reran": False},
        "verifier": {"version": verifier_version},
        "source": {"outcome_kind": source["outcome_kind"],
                   "adapter_id": source["adapter_id"],
                   "adapter_version": source["adapter_version"]},
        "matches_recording": divergence is None,
        "divergence": deepcopy(divergence) if divergence is not None else None,
        "trace": deepcopy(observed.trace),
        "state": deepcopy(dict(observed.state)),
        "checks": _check_dicts(env, observed.trace, observed.state),
        "tool_calls": len(observed.trace),
    }


def summarize(record: Mapping[str, Any]) -> str:
    """One log line for an evidence record, naming the target version."""
    if not isinstance(record, Mapping):
        raise ValueError("Invalid evidence record")
    kind = record.get("outcome_kind")
    target = record.get("target")
    if kind not in OUTCOME_KINDS or not isinstance(target, Mapping):
        raise ValueError("Invalid evidence record")
    base = (f"[p3-self-play] {kind} "
            f"target={target.get('adapter_id')}@{target.get('adapter_version')}")
    if kind == AGENT_RETEST:
        if "checks_passed" in record:
            return base + (" checks_passed" if record["checks_passed"] else " checks_failed")
        return base + f" status={record.get('status')} stopped={record.get('stopped')}"
    if kind == RECORDED_REPLAY:
        return base + (" match" if record.get("matches_recording") else " MISMATCH")
    denial = record.get("denial")
    code = denial.get("code") if isinstance(denial, Mapping) else denial
    return base + f" denied={code}"
