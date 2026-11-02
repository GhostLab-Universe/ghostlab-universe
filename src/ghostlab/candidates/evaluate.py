"""Deterministic parent-vs-candidate evaluation harness (stdlib only).

:func:`run_candidate_evaluation` runs the parent and the candidate
targets from the operator registry against the original failing
scenario plus caller-supplied development regressions, each side on
fresh environment instances, and assembles a multidimensional
evaluation record: original scenario, regressions, completion, fault
coverage and usage. Every dimension carries its own per-side detail;
no top-level aggregate verdict hides a failing dimension. Usage
counts tool calls only -- wall-clock time is never measured, so
results are deterministic. Zero model calls, no network.
"""

from datetime import datetime, timezone

from ..extensions.runner import run_attempt
from ..extensions.target import TargetTask
from .records import (CANDIDATE_SCHEMA_VERSION, digest_of, evaluation_id,
                      validate_evaluation)


def _allow_all(action):
    return None


def _run_side(*, registry, world_version, target_id, case, cap, authorize):
    env = registry.environment_factory(world_version)()
    task_node = case["task"]
    task = TargetTask(task_id=task_node["task_id"],
                      params=dict(task_node["params"]))
    env.reset(dict(case["fixture"]), dict(case["scenario"]),
              actor_id=case["actor_id"])
    target = registry.target_factory(world_version, target_id)()
    result = run_attempt(env, target, task, authorize=authorize, cap=cap)
    checks = [{"name": check.name, "passed": check.passed}
              for check in env.evaluate(trace=result.trace,
                                        state=dict(result.state))]
    denial = result.denial
    if denial is not None:
        denial = {"code": getattr(denial, "code", "denied"),
                  "reason": getattr(denial, "reason", str(denial))}
    return {
        "attempt": {"status": result.status, "tool_calls": result.tool_calls,
                    "stopped": result.stopped, "denial": denial,
                    "checks": checks},
        "faults_fired": sorted(env.faults_fired(dict(case["scenario"]),
                                                 result.trace)),
    }


def _check_case(case, label: str) -> dict:
    if type(case) is not dict:
        raise ValueError(f"invalid {label}")
    for key in ("case_id", "fixture", "scenario", "actor_id", "task"):
        if key not in case:
            raise ValueError(f"{label} needs {key}")
    task = case["task"]
    if (type(task) is not dict or type(task.get("task_id")) is not str
            or not task.get("task_id") or type(task.get("params")) is not dict):
        raise ValueError(f"invalid {label}.task")
    if (type(case["fixture"]) is not dict or type(case["scenario"]) is not dict
            or type(case["actor_id"]) is not str or not case["actor_id"]
            or type(case["case_id"]) is not str or not case["case_id"]):
        raise ValueError(f"invalid {label}")
    return case


def run_candidate_evaluation(*, registry, world_version: str,
                             project_id: str, candidate_id: str,
                             parent_id: str, proposed_id: str,
                             original: dict, regressions=(),
                             cap: int = 20, authorize=None,
                             evaluated_by: str,
                             now_iso: str | None = None) -> dict:
    """Run parent vs candidate and return a validated evaluation record.

    ``original`` is the failing scenario the candidate must fix;
    ``regressions`` is a list of development-regression cases. Each case
    carries ``case_id``, ``fixture``, ``scenario``, ``actor_id`` and
    ``task`` (``task_id`` plus world-defined ``params``). Both sides run
    every case on fresh instances; the record keeps every dimension
    separate. ``cap`` bounds applied actions per attempt.
    """
    if type(project_id) is not str or not project_id:
        raise ValueError("invalid project_id")
    if type(candidate_id) is not str or not candidate_id:
        raise ValueError("invalid candidate_id")
    if type(parent_id) is not str or not parent_id:
        raise ValueError("invalid parent_id")
    if type(proposed_id) is not str or not proposed_id:
        raise ValueError("invalid proposed_id")
    if type(cap) is not int or not 1 <= cap <= 1000:
        raise ValueError("cap must be an integer from 1 to 1000")
    if type(evaluated_by) is not str or not evaluated_by:
        raise ValueError("invalid evaluated_by")
    authorize = _allow_all if authorize is None else authorize
    if not callable(authorize):
        raise TypeError("authorize must be callable")
    _check_case(original, "original")
    regressions = list(regressions)
    for index, case in enumerate(regressions):
        _check_case(case, f"regressions[{index}]")
    stamp = now_iso or datetime.now(timezone.utc).isoformat()

    def attempt_for(case):
        parent = _run_side(registry=registry, world_version=world_version,
                           target_id=parent_id, case=case, cap=cap,
                           authorize=authorize)
        candidate = _run_side(registry=registry, world_version=world_version,
                              target_id=proposed_id, case=case, cap=cap,
                              authorize=authorize)
        return {
            "case_id": case["case_id"],
            "scenario_ref": {"fixture": dict(case["fixture"]),
                             "scenario": dict(case["scenario"]),
                             "actor_id": case["actor_id"]},
            "parent": parent["attempt"],
            "candidate": candidate["attempt"],
            "_faults": {"parent": parent["faults_fired"],
                        "candidate": candidate["faults_fired"]},
        }

    original_full = attempt_for(original)
    regression_full = [attempt_for(case) for case in regressions]

    def strip(case):
        return {k: v for k, v in case.items() if k != "_faults"}

    scenario_faults = sorted({key for case in [original, *regressions]
                              for key, value in case["scenario"].items()
                              if value is True})
    all_cases = [original_full, *regression_full]
    completion = {side: {"status": original_full[side]["status"],
                         "tool_calls": original_full[side]["tool_calls"],
                         "stopped": original_full[side]["stopped"]}
                  for side in ("parent", "candidate")}
    fault_coverage = {"scenario_faults": scenario_faults}
    for side in ("parent", "candidate"):
        fault_coverage[side] = {"faults_fired": [
            {"case_id": case["case_id"], "fired": case["_faults"][side]}
            for case in all_cases]}
    usage = {"cap": cap}
    for side in ("parent", "candidate"):
        usage[side] = {"tool_calls": sum(case[side]["tool_calls"]
                                         for case in all_cases)}
    body = {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "evaluation_id": "pending",
        "candidate_id": candidate_id,
        "project_id": project_id,
        "original": strip(original_full),
        "regressions": [strip(case) for case in regression_full],
        "completion": completion,
        "fault_coverage": fault_coverage,
        "usage": usage,
        "evaluated_by": evaluated_by,
        "evaluated_at": stamp,
    }
    stable = {k: v for k, v in body.items()
              if k not in ("evaluation_id", "digest")}
    body["evaluation_id"] = evaluation_id(project_id, candidate_id, stable)
    body["digest"] = digest_of({**body, "digest": None})
    return validate_evaluation(body)


__all__ = ["run_candidate_evaluation"]
