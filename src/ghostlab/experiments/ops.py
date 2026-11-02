"""Live experiment operations: create, resolve, run, compare.

This module wires R2 configs and the project-scoped store onto P1
execution: operator-gated environments (``load_into`` refuses unknown,
code-changed, unqualified or failed worlds), the generic
:func:`run_attempt` loop, and invariant checks evaluated on a fresh
adapter from the recorded evidence.

Execution honesty rules:

- Running rechecks the execution gate at start. A changed adapter never
  inherits an old qualification.
- ``authorize`` allows exactly the tools pinned at resolve time
  (``pins.discovered_tools``). A denial ends the attempt without
  touching the world and is recorded on the run.
- Verdicts come from ``evaluate`` over the recorded trace and state.
  ``failed_rules`` intersects failing invariants with the selected
  rules; the full check map stays on the run.
- ``faults_fired`` is reported only when the world exposes
  ``faults_fired(scenario, trace)``; otherwise it is None with
  ``faults_status`` set to ``unreported-by-world``. A required fault
  whose firing is unreported leaves the condition untested, never a
  clean pass.
- World errors during reset or the loop are recorded as failed runs
  with null evidence. Partial tool counts are then unknown: the run
  says so instead of inventing a number. Verdict-assembly failures
  (evaluate, fired-fault reporting, evidence serialization, secret
  scan) are likewise recorded as failed runs, but with the known
  tool count: the world ran, so the run exists.
"""

import json
import time
from copy import deepcopy

from ..extensions.environment import Action
from ..extensions.qualify import ScriptTarget
from ..extensions.runner import run_attempt
from ..extensions.target import TargetTask
from ..replay import allow_tools
from .configs import (MAX_CONFIG_BYTES, ConfigError, canonical_digest,
                      check_project_id, scan_for_secrets, validate_policy,
                      validate_requested, validate_resolved,
                      resolve_requested)
from .diffs import diff_resolved
from .store import MAX_EVIDENCE_BYTES

SCRIPT_ADAPTER_ID = "exp-script"

_RUN_KEYS = {"status", "termination_reason", "tool_calls", "wall_clock_ms",
             "failed_rules", "checks", "faults_configured", "faults_fired",
             "faults_status", "denial", "limits_exceeded",
             "evidence_digest", "evidence"}
_RUN_STATUSES = {"completed", "failed", "incomplete"}


class RunRefused(ValueError):
    """Execution refused before the world ran (gate, budget, duplicate)."""


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def validate_run_body(body: dict) -> dict:
    """Validate a run record body before storage (pure)."""
    if type(body) is not dict or set(body) != _RUN_KEYS:
        raise ConfigError("run body must carry exactly the v1 run fields")
    if body["status"] not in _RUN_STATUSES:
        raise ConfigError("invalid run status")
    if type(body["termination_reason"]) is not str or not body["termination_reason"]:
        raise ConfigError("invalid run termination_reason")
    if type(body["tool_calls"]) is not int or body["tool_calls"] < 0:
        raise ConfigError("invalid run tool_calls")
    if type(body["wall_clock_ms"]) is not int or body["wall_clock_ms"] < 0:
        raise ConfigError("invalid run wall_clock_ms")
    if (type(body["failed_rules"]) is not list
            or any(type(name) is not str for name in body["failed_rules"])
            or sorted(body["failed_rules"]) != body["failed_rules"]):
        raise ConfigError("invalid run failed_rules")
    if (type(body["checks"]) is not dict
            or any(type(hit) is not bool for hit in body["checks"].values())):
        raise ConfigError("invalid run checks")
    if (type(body["faults_configured"]) is not list
            or any(type(name) is not str for name in body["faults_configured"])):
        raise ConfigError("invalid run faults_configured")
    if body["faults_fired"] is not None and (
            type(body["faults_fired"]) is not list
            or any(type(name) is not str for name in body["faults_fired"])):
        raise ConfigError("invalid run faults_fired")
    if body["faults_status"] not in ("reported", "unreported-by-world"):
        raise ConfigError("invalid run faults_status")
    if body["denial"] is not None and (
            type(body["denial"]) is not str or not body["denial"]):
        raise ConfigError("invalid run denial")
    if (type(body["limits_exceeded"]) is not list
            or any(type(name) is not str for name in body["limits_exceeded"])):
        raise ConfigError("invalid run limits_exceeded")
    if body["evidence_digest"] is not None and (
            type(body["evidence_digest"]) is not str
            or not body["evidence_digest"]):
        raise ConfigError("invalid run evidence_digest")
    if body["evidence"] is not None and type(body["evidence"]) is not dict:
        raise ConfigError("invalid run evidence")
    return deepcopy(body)


def create_requested(store, *, project_id: str, body: dict, requested_by: str,
                     policy, created_at=None):
    """Validate and persist a reusable requested config. No execution."""
    check_project_id(project_id)
    if type(requested_by) is not str or not requested_by:
        raise ConfigError("invalid requested_by")
    clean_policy = validate_policy(policy)
    clean = validate_requested(body, clean_policy)
    scan_for_secrets(clean, "requested config")
    text = json.dumps(clean, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)
    if len(text.encode()) > MAX_CONFIG_BYTES:
        raise ConfigError("requested config exceeds the size bound")
    return store.save_requested(
        project_id=project_id, body=clean, digest=canonical_digest(clean),
        requested_by=requested_by, created_at=created_at or _utcnow())


def resolve_config(store, *, project_id: str, requested_id: str, policy,
                   operators, targets, manifest_digest=None,
                   resolved_by="controller", created_at=None):
    """Resolve a stored request to pinned versions. Describe-only."""
    check_project_id(project_id)
    record = store.get_requested(project_id, requested_id)
    resolved = resolve_requested(
        record["config"], project_id=project_id, requested_id=requested_id,
        requested_digest=record["digest"], requested_by=record["requested_by"],
        policy=policy, operators=operators, targets=targets,
        manifest_digest=manifest_digest, resolved_by=resolved_by)
    scan_for_secrets(resolved, "resolved config")
    return store.save_resolved(
        project_id=project_id, requested_id=requested_id, body=resolved,
        digest=resolved["digest"], created_at=created_at or _utcnow())


def _build_target(resolved, targets):
    target = resolved["effective"]["target"]
    world = resolved["pins"]["world_version"]
    if target["kind"] == "registered":
        try:
            return targets.target_factory(world, target["id"])()
        except (KeyError, LookupError):
            raise RunRefused(f"target '{target['id']}' is no longer "
                             "registered") from None
    try:
        actions = [Action(tool=item["tool"], arguments=dict(item["arguments"]))
                   for item in target["actions"]]
    except (KeyError, TypeError, ValueError):
        raise RunRefused("stored script target is malformed") from None
    return ScriptTarget(actions, adapter_id=SCRIPT_ADAPTER_ID)


def run_repetition(store, *, project_id: str, resolved_id: str,
                   repetition: int, attempt: int, run_by: str,
                   operators, targets, created_at=None):
    """Execute one repetition attempt and record the run.

    Refuses duplicates, exhausted aggregate budgets and unqualified
    worlds BEFORE the world runs. One attempt runs once: retries use a
    new attempt number.
    """
    check_project_id(project_id)
    if type(run_by) is not str or not run_by:
        raise ConfigError("invalid run_by")
    if type(repetition) is not int or repetition < 1:
        raise ConfigError("invalid repetition")
    if type(attempt) is not int or attempt < 1:
        raise ConfigError("invalid attempt")
    record = store.get_resolved(project_id, resolved_id)
    resolved = validate_resolved(record["resolved"])
    effective = resolved["effective"]
    if repetition > effective["repetitions"]["count"]:
        raise RunRefused("repetition exceeds the resolved count")
    if store.attempt_recorded(resolved_id, repetition, attempt):
        raise RunRefused(f"attempt {attempt} of repetition {repetition} is "
                         "already recorded; retries use a new attempt number")
    per_run = effective["limits"]["max_tool_calls"]
    if (store.budget_used(project_id, resolved_id, "tool_calls") + per_run
            > effective["limits"]["max_total_tool_calls"]):
        raise RunRefused("aggregate tool budget cannot cover another run")
    world = resolved["pins"]["world_version"]
    try:
        env_factory = operators.resolve_class(world)
    except (LookupError, ValueError) as error:
        raise RunRefused(str(error)) from None
    authorize = allow_tools(*resolved["pins"]["discovered_tools"])
    task = TargetTask(task_id=f"{resolved_id}:rep{repetition}:att{attempt}",
                      params={})
    faults_configured = [fault["hook"] for fault in effective["faults"]]
    started = time.monotonic()

    def finish(body):
        wall = int((time.monotonic() - started) * 1000)
        body["wall_clock_ms"] = wall
        if wall > effective["limits"]["max_wall_clock_ms"]:
            body["limits_exceeded"].append("max_wall_clock_ms")
        validate_run_body(body)
        stamp = created_at or _utcnow()
        return store.save_run_with_ledger(
            project_id=project_id, resolved_id=resolved_id,
            repetition=repetition, attempt=attempt, body=body,
            tool_calls=body["tool_calls"], wall_clock_ms=wall,
            created_at=stamp)

    def failed(reason, calls=0):
        return finish({
            "status": "failed", "termination_reason": reason,
            "tool_calls": calls,
            "wall_clock_ms": 0, "failed_rules": [], "checks": {},
            "faults_configured": faults_configured, "faults_fired": None,
            "faults_status": "unreported-by-world", "denial": None,
            "limits_exceeded": [], "evidence_digest": None, "evidence": None})

    target = _build_target(resolved, targets)
    try:
        env = env_factory()
        try:
            snapshot = env.named_fixture(effective["fixture"]["name"])
        except KeyError:
            env.close()
            return failed("world-error:fixture-unknown (calls unknown)")
        except Exception:
            env.close()
            raise
        try:
            env.reset(dict(snapshot), dict(effective["scenario"]),
                      actor_id=run_by)
        except Exception:
            env.close()
            return failed("world-error:reset-rejected (calls unknown)")
        attempt_result = run_attempt(env, target, task, authorize=authorize,
                                     cap=per_run)
    except Exception as error:
        return failed(f"world-error:{type(error).__name__} (calls unknown)")

    try:
        checks: dict = {}
        fired = None
        faults_status = "unreported-by-world"
        evaluator = env_factory()
        try:
            verdicts = evaluator.evaluate(trace=attempt_result.trace,
                                          state=attempt_result.state)
            checks = {check.name: check.passed for check in verdicts}
            reporter = getattr(evaluator, "faults_fired", None)
            if callable(reporter):
                fired = sorted(reporter(dict(effective["scenario"]),
                                        list(attempt_result.trace)))
                faults_status = "reported"
        finally:
            evaluator.close()

        selected = set(effective["rules"])
        failed_rules = sorted(name for name, hit in checks.items()
                              if hit is False and name in selected)
        evidence = _jsonable({"trace": attempt_result.trace,
                              "state": attempt_result.state})
        if len(json.dumps(evidence).encode()) > MAX_EVIDENCE_BYTES:
            evidence = None
        if evidence is not None:
            scan_for_secrets(evidence, "run evidence")
        if attempt_result.status == "budget_exhausted":
            status, termination = "incomplete", "budget-exhausted"
            over = ["max_tool_calls"]
        elif attempt_result.denial is not None:
            status, termination, over = "completed", "denied-by-authorize", []
        else:
            status, termination, over = "completed", "target-finished", []
        body = {
            "status": status, "termination_reason": termination,
            "tool_calls": attempt_result.tool_calls, "wall_clock_ms": 0,
            "failed_rules": failed_rules, "checks": checks,
            "faults_configured": faults_configured, "faults_fired": fired,
            "faults_status": faults_status,
            "denial": None if attempt_result.denial is None
            else str(attempt_result.denial)[:500],
            "limits_exceeded": over,
            "evidence_digest": None if evidence is None
            else canonical_digest(evidence),
            "evidence": evidence}
    except Exception as error:
        return failed(
            f"world-error:verdict-assembly:{type(error).__name__}",
            calls=attempt_result.tool_calls)
    return finish(body)

def compare_resolved(store, *, project_id: str, left_id: str, right_id: str):
    """Diff two stored resolved configs (comparison metadata)."""
    check_project_id(project_id)
    left = store.get_resolved(project_id, left_id)["resolved"]
    right = store.get_resolved(project_id, right_id)["resolved"]
    return diff_resolved(left, right)


__all__ = ["RunRefused", "SCRIPT_ADAPTER_ID", "compare_resolved",
           "create_requested", "resolve_config", "run_repetition",
           "validate_run_body"]
