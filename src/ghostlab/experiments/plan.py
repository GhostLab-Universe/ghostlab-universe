"""Exploration planner: bounded pre-execution plans over discovered capabilities.

Track T3. Strategies:

- ``fixed``: caller-supplied requested configs, used verbatim.
- ``stateful_random``: seeded stdlib sampling over DISCOVERED tools/hooks only
  (``envs.capabilities.discover``; never invented), bounded count,
  deterministic per seed.
- ``model_guided``: an Explorer object proposes requested configs via
  ``propose(findings) -> [proposal dicts]``; EVERY proposal is re-validated
  through requested-config validation at execution time and invalid proposals
  are rejected, never executed. Only scripted explorers ship here: zero live
  model calls.

Execution goes through the PUBLIC ``experiments.ops`` boundary only, never
config/controller internals, so concurrent track edits cannot break this
module. Scenario records carry ``source`` (``dev`` or ``heldout``); heldout
execution is refused because no owner interface exists. Standard library only.
"""

import random
import re
from copy import deepcopy

from ..envs.capabilities import discover
from . import ops

PLAN_VERSION = 1
STRATEGIES = ("fixed", "stateful_random", "model_guided")
SOURCES = ("dev", "heldout")

MAX_SEEDS = 16
MAX_SCHEDULES_PER_SEED = 25
MAX_FIXED_BODIES = 64
MAX_PLANNED_RUNS = 625
MAX_HOOKS_PER_SPACE = 16

# Mirrors of the v1 requested-config structural bounds. The ops boundary stays
# authoritative: any drift here fails closed at execution time.
MIN_TRIGGER_OCCURRENCE = 1
MAX_TRIGGER_OCCURRENCE = 64
MIN_REPETITIONS = 1
MAX_REPETITIONS = 25

_ENV_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_HOOK_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_PLAN_KEYS = {"plan_version", "name", "env_id", "fixture", "agent", "task",
              "checks", "fault_space", "limits", "repetitions", "strategy",
              "seeds", "requested_configs", "explorer", "policy", "source"}
_FIXTURE_KEYS = {"order_id", "customer_id", "paid_minor", "currency"}
_LIMITS_KEYS = {"max_tool_calls", "max_total_tool_calls", "max_wall_clock_ms"}
_SPACE_KEYS = {"hooks", "tools", "occurrences", "count", "required"}

# Config-error text that means "the environment cannot do this" (unsupported)
# rather than "the request itself was malformed" (failed). Classification only
# affects the per-fault outcome label; both are recorded, never executed.
_UNSUPPORTED_TOKENS = ("unsupported", "not implemented", "not allowed",
                       "unknown", "not qualified", "mismatch")


class PlanError(ValueError):
    """A plan is malformed, unbounded, or refused (heldout)."""


class ScriptedExplorer:
    """Canned explorer for tests: replays scripted proposals, zero live calls."""

    def __init__(self, proposals) -> None:
        if type(proposals) is not list:
            raise PlanError("scripted proposals must be a list")
        self._proposals = deepcopy(proposals)

    def propose(self, findings):
        """Return the scripted proposals; findings must be a list."""
        if type(findings) is not list:
            raise PlanError("explorer findings must be a list")
        return deepcopy(self._proposals)


def _check_name(value, path: str, limit: int = 100) -> str:
    if type(value) is not str or not value or len(value) > limit:
        raise PlanError(f"invalid {path}")
    return value


def _check_fixture(fixture, env_id="refund-env") -> dict:
    if env_id != "refund-env":
        # Non-refund envs validate through their registered driver, as configs does.
        from .configs import ConfigError, _validate_fixture
        try:
            return deepcopy(_validate_fixture(fixture, env_id))
        except ConfigError as error:
            raise PlanError(str(error)) from error
    if type(fixture) is not dict or set(fixture) != _FIXTURE_KEYS:
        raise PlanError("fixture must carry exactly order, customer,"
                        " paid_minor and currency")
    for key in ("order_id", "customer_id", "currency"):
        _check_name(fixture[key], f"fixture.{key}")
    paid = fixture["paid_minor"]
    if type(paid) is not int or not 0 < paid <= 10 ** 12:
        raise PlanError("invalid fixture.paid_minor")
    return deepcopy(fixture)


def _check_checks(checks) -> list:
    if (type(checks) is not list or not 1 <= len(checks) <= 8
            or any(type(c) is not str or not c or len(c) > 100
                   for c in checks)
            or len(set(checks)) != len(checks)):
        raise PlanError("checks must list 1-8 unique non-empty rule names;"
                        " the ops boundary validates them against known"
                        " invariants at execution time")
    return list(checks)


def _check_limits(limits) -> dict:
    if type(limits) is not dict or set(limits) != _LIMITS_KEYS:
        raise PlanError("limits must carry exactly max_tool_calls,"
                        " max_total_tool_calls and max_wall_clock_ms")
    for key in _LIMITS_KEYS:
        if type(limits[key]) is not int or limits[key] <= 0:
            raise PlanError(f"limits.{key} must be a positive int")
    if limits["max_tool_calls"] > limits["max_total_tool_calls"]:
        raise PlanError("impossible limits: max_tool_calls exceeds"
                        " max_total_tool_calls")
    return dict(limits)


def _check_space(space, strategy: str) -> dict:
    if strategy != "stateful_random":
        if space not in (None, {}, []):
            raise PlanError(f"fault_space must be empty for {strategy};"
                            " fixed uses requested_configs, guided uses"
                            " explorer proposals")
        return {}
    if type(space) is not dict or not set(space) <= _SPACE_KEYS:
        raise PlanError("random fault_space must only carry hooks, tools,"
                        " occurrences, count and required")
    hooks = space.get("hooks", [])
    if (type(hooks) is not list or len(hooks) > MAX_HOOKS_PER_SPACE
            or any(type(h) is not str or not _HOOK_PATTERN.match(h)
                   for h in hooks)
            or len(set(hooks)) != len(hooks)):
        raise PlanError("space hooks must list 0-16 unique hook names")
    tools = space.get("tools", ["*"])
    if (type(tools) is not list or not 1 <= len(tools) <= 16
            or any(t != "*" and (type(t) is not str
                                 or not _HOOK_PATTERN.match(t))
                   for t in tools)
            or len(set(tools)) != len(tools)):
        raise PlanError("space tools must list 1-16 unique names or '*'")
    occurrences = space.get("occurrences", [1])
    if (type(occurrences) is not list or not 1 <= len(occurrences) <= 8
            or any(type(o) is not int
                   or not MIN_TRIGGER_OCCURRENCE <= o
                   <= MAX_TRIGGER_OCCURRENCE for o in occurrences)
            or len(set(occurrences)) != len(occurrences)):
        raise PlanError("space occurrences must list 1-8 unique ints 1-64")
    count = space.get("count", 4)
    if type(count) is not int \
            or not 1 <= count <= MAX_SCHEDULES_PER_SEED:
        raise PlanError("space count must be"
                        f" 1-{MAX_SCHEDULES_PER_SEED}")
    if type(space.get("required", True)) is not bool:
        raise PlanError("space required must be boolean")
    return {"hooks": list(hooks), "tools": list(tools),
            "occurrences": sorted(occurrences), "count": count,
            "required": space.get("required", True)}


def build_plan(env_id, fixture, agent, task, checks, fault_space, limits,
               repetitions, strategy, seeds, *, requested_configs=None,
               explorer=None, policy=None, source="dev",
               name="exploration-plan") -> dict:
    """Build a bounded exploration plan (pure; no discovery, no execution).

    ``requested_configs`` is required for ``fixed`` (verbatim bodies) and
    rejected otherwise; ``explorer`` (an object with
    ``propose(findings) -> [proposal dicts]``) is required for
    ``model_guided`` and rejected otherwise. ``policy`` is stored verbatim for
    execution-time validation; the ops boundary stays authoritative.
    """
    if type(env_id) is not str or not _ENV_ID_PATTERN.match(env_id):
        raise PlanError("invalid env_id")
    checked_fixture = _check_fixture(fixture, env_id)
    _check_name(agent, "agent")
    if type(task) is not str or not task or len(task) > 2000:
        raise PlanError("task must be a non-empty string up to 2000 chars")
    checked_checks = _check_checks(checks)
    checked_limits = _check_limits(limits)
    if type(repetitions) is not int \
            or not MIN_REPETITIONS <= repetitions <= MAX_REPETITIONS:
        raise PlanError("repetitions must be"
                        f" {MIN_REPETITIONS}-{MAX_REPETITIONS}")
    if strategy not in STRATEGIES:
        raise PlanError(f"unknown strategy {strategy!r}:"
                        f" want one of {', '.join(STRATEGIES)}")
    if (type(seeds) is not list or not 1 <= len(seeds) <= MAX_SEEDS
            or any(type(s) is not int or s < 0 for s in seeds)):
        raise PlanError(f"seeds must list 1-{MAX_SEEDS} non-negative ints")
    if source not in SOURCES:
        raise PlanError(f"unknown source {source!r}: want dev or heldout")
    _check_name(name, "name")
    space = _check_space(fault_space, strategy)
    if strategy == "fixed":
        if (type(requested_configs) is not list
                or not 1 <= len(requested_configs) <= MAX_FIXED_BODIES):
            raise PlanError("fixed plans need 1-64 requested configs")
        if any(type(b) is not dict for b in requested_configs):
            raise PlanError("fixed requested configs must all be mappings")
        if explorer is not None:
            raise PlanError("fixed plans take requested_configs, not"
                            " an explorer")
        stored_configs = deepcopy(requested_configs)
    else:
        if requested_configs is not None:
            raise PlanError(f"{strategy} plans generate configs; pass no"
                            " requested_configs")
        stored_configs = None
    if strategy == "model_guided":
        if explorer is None or not callable(getattr(explorer, "propose",
                                                    None)):
            raise PlanError("model_guided plans need an explorer with"
                            " propose(findings)")
    elif explorer is not None:
        raise PlanError(f"{strategy} plans take no explorer")
    return {"plan_version": PLAN_VERSION, "name": name, "env_id": env_id,
            "fixture": checked_fixture, "agent": agent, "task": task,
            "checks": checked_checks, "fault_space": space,
            "limits": checked_limits, "repetitions": repetitions,
            "strategy": strategy, "seeds": list(seeds),
            "requested_configs": stored_configs, "explorer": explorer,
            "policy": deepcopy(policy) if policy is not None else None,
            "source": source}


def _check_plan(plan) -> dict:
    if type(plan) is not dict or set(plan) != _PLAN_KEYS:
        raise PlanError("plan must carry exactly the v1 plan fields")
    if plan["plan_version"] != PLAN_VERSION:
        raise PlanError("unsupported plan_version")
    return plan


def _discover(registry, env_id: str) -> dict:
    try:
        found = discover(registry, env_id)
    except ValueError as error:
        raise PlanError(f"capability discovery failed: {error}") from error
    if not found["match"]:
        raise PlanError("env declaration mismatch:"
                        f" {', '.join(found['mismatches'])}")
    return found


def _body_repetitions(body, where: str) -> int:
    reps = body.get("repetitions")
    count = reps.get("count") if type(reps) is dict else None
    if type(count) is not int or count < 1:
        raise PlanError(f"{where} needs repetitions.count >= 1 for"
                        " pre-execution counting")
    return count


def _random_bodies(plan) -> tuple:
    space = plan["fault_space"]
    bodies = []
    for seed in plan["seeds"]:
        rng = random.Random(seed)
        for index in range(space["count"]):
            hook = rng.choice(space["_supported_hooks"])
            tool = rng.choice(sorted(space["_supported_tools"]))
            occurrence = rng.choice(space["occurrences"])
            bodies.append({
                "config_version": 1,
                "name": f"{plan['name']}-s{seed}-{index}",
                "env_id": plan["env_id"], "agent": plan["agent"],
                "fixture": deepcopy(plan["fixture"]),
                "faults": [{"hook": hook, "params": {},
                            "required": space["required"],
                            "trigger": {"type": "occurrence", "tool": tool,
                                        "occurrence": occurrence}}],
                "rules": list(plan["checks"]),
                "limits": dict(plan["limits"]),
                "repetitions": {"count": plan["repetitions"]}})
    return bodies


def _plan_bodies(plan, registry, findings):
    """Return (bodies, expand_rejections, coverage_space).

    Bodies are ordered requested-config dicts. Guided proposals that are not
    mappings (or carry no countable repetitions) are rejected here, never
    executed; every mapping still faces ops validation at execution time.
    """
    strategy = plan["strategy"]
    rejected = []
    if strategy == "fixed":
        bodies = []
        for index, body in enumerate(plan["requested_configs"]):
            _body_repetitions(body, f"requested_configs[{index}]")
            bodies.append(deepcopy(body))
        return bodies, rejected, None
    if strategy == "stateful_random":
        if registry is None:
            raise PlanError("stateful_random expansion needs a registry:"
                            " sampling runs over discovered tools/hooks only")
        found = _discover(registry, plan["env_id"])
        discovered_hooks = list(found["fault_hooks"])
        discovered_tools = sorted(t["name"]
                                  for t in found["discovered_tools"])
        wanted_hooks = plan["fault_space"]["hooks"] or discovered_hooks
        supported = [h for h in wanted_hooks if h in discovered_hooks]
        unsupported = [h for h in wanted_hooks if h not in discovered_hooks]
        if not supported:
            raise PlanError("no supported hooks to sample: every requested"
                            f" hook is undiscovered: {unsupported}")
        space = dict(plan["fault_space"])
        space["_supported_hooks"] = sorted(supported)
        space["_supported_tools"] = sorted(
            set(discovered_tools) | {"*"}) \
            if "*" in space["tools"] else sorted(
                t for t in space["tools"] if t in discovered_tools)
        if not space["_supported_tools"]:
            raise PlanError("no supported trigger tools to sample")
        plan = dict(plan, fault_space=space)
        return _random_bodies(plan), rejected, {
            "hooks": sorted(supported),
            "tools": sorted(set(discovered_tools) | {"*"}),
            "occurrences": list(space["occurrences"]),
            "unsupported_hooks": unsupported,
            "unsupported_tools": [t for t in space["tools"]
                                  if t != "*" and t not in discovered_tools]}
    proposals = plan["explorer"].propose(deepcopy(findings))
    if type(proposals) is not list:
        raise PlanError("explorer.propose must return a list of proposals")
    bodies = []
    for index, proposal in enumerate(proposals):
        where = f"proposal[{index}]"
        if type(proposal) is not dict:
            rejected.append({"stage": "expand", "unit_index": None,
                             "proposal_index": index,
                             "reason": f"{where} is not a mapping;"
                                       " rejected, never executed"})
            continue
        try:
            _body_repetitions(proposal, where)
        except PlanError as error:
            rejected.append({"stage": "expand", "unit_index": None,
                             "proposal_index": index,
                             "reason": f"{error}; rejected, never executed"})
            continue
        bodies.append(deepcopy(proposal))
    if not bodies and not rejected:
        raise PlanError("explorer proposed nothing")
    return bodies, rejected, None


def _planned_fault_cells(bodies):
    """Map each body to coverage cells (hook, tool, occurrence), defensively."""
    entries = []
    for body in bodies:
        faults = body.get("faults") if type(body) is dict else None
        cells = set()
        if type(faults) is list:
            for fault in faults:
                if type(fault) is not dict \
                        or type(fault.get("hook")) is not str:
                    continue
                trigger = fault.get("trigger")
                if type(trigger) is dict:
                    tool = trigger.get("tool", "*")
                    occurrence = trigger.get("occurrence")
                else:
                    tool, occurrence = "*", None
                if type(tool) is not str:
                    tool = "*"
                cells.add((fault["hook"], tool, occurrence))
        entries.append(cells)
    return entries


def _pre_execution_report(plan, registry, bodies, units, rejected, space):
    per_body_cells = _planned_fault_cells(bodies)
    if registry is not None:
        found = _discover(registry, plan["env_id"])
        hooks_dim = sorted(found["fault_hooks"])
        tools_dim = sorted({t["name"] for t in found["discovered_tools"]}
                           | {"*"})
        supported_source = "discovered"
    else:
        hooks_dim = sorted({cell[0] for cells in per_body_cells
                            for cell in cells})
        tools_dim = sorted({cell[1] for cells in per_body_cells
                            for cell in cells} | {"*"})
        supported_source = "planned"
    if space is not None:
        hooks_dim = list(space["hooks"])
        tools_dim = list(space["tools"])
        occ_dim: list = list(space["occurrences"])
    else:
        occ_dim = sorted({cell[2] for cells in per_body_cells
                          for cell in cells
                          if type(cell[2]) is int})
        if any(cell[2] is None for cells in per_body_cells
               for cell in cells) or not occ_dim:
            occ_dim = occ_dim + [None]
    covered = {}
    body_runs = []
    for body in bodies:
        body_runs.append(_body_repetitions(body, "planned body"))
    for body_index, cells in enumerate(per_body_cells):
        for cell in cells:
            covered[cell] = covered.get(cell, 0) + body_runs[body_index]
    full_cells = []
    for hook in hooks_dim:
        for tool in tools_dim:
            for occurrence in occ_dim:
                full_cells.append({
                    "hook": hook, "tool": tool, "occurrence": occurrence,
                    "planned_runs": covered.get((hook, tool, occurrence),
                                                0)})
    unsupported = []
    if space is not None:
        for hook in space["unsupported_hooks"]:
            unsupported.append({"kind": "hook", "value": hook,
                                "reason": "requested hook is not implemented"
                                          " by the env; never generated"})
        for tool in space["unsupported_tools"]:
            unsupported.append({"kind": "tool", "value": tool,
                                "reason": "requested trigger tool is not"
                                          " implemented by the env"})
    elif registry is not None:
        discovered_hooks = set(found["fault_hooks"])
        discovered_tools = {t["name"] for t in found["discovered_tools"]}
        for cell in {cell for cells in per_body_cells for cell in cells}:
            if cell[0] not in discovered_hooks:
                unsupported.append(
                    {"kind": "hook", "value": cell[0],
                     "reason": "planned hook is not implemented by the env"})
            elif cell[1] != "*" and cell[1] not in discovered_tools:
                unsupported.append(
                    {"kind": "tool", "value": cell[1],
                     "reason": "planned trigger tool is not implemented"
                               " by the env"})
        unsupported.sort(key=lambda e: (e["kind"], e["value"]))
    return {"plan_name": plan["name"], "strategy": plan["strategy"],
            "source": plan["source"],
            "planned_run_count": len(units),
            "planned_configs": len(bodies),
            "supported_source": supported_source,
            "coverage": {
                "hooks": hooks_dim, "tools": tools_dim,
                "occurrences": occ_dim, "cells": full_cells,
                "total_cells": len(full_cells),
                "covered_cells": sum(1 for c in full_cells
                                     if c["planned_runs"] > 0)},
            "unsupported_conditions": unsupported,
            "rejected_proposals": rejected}


def expand_plan(plan, registry=None, findings=None) -> dict:
    """Expand a plan into ordered units plus a pre-execution report.

    Returns ``{"units", "report"}`` where units are ordered
    ``(requested_body, repetition, attempt)`` tuples and the report carries
    ``planned_run_count``, the tools x hooks x occurrences coverage matrix
    over the supported space, and ``unsupported_conditions``. Pure
    pre-execution: no configs are stored and nothing runs. ``findings``
    defaults to ``[]`` for guided explorers.
    """
    body = _check_plan(plan)
    if findings is None:
        findings = []
    if type(findings) is not list:
        raise PlanError("findings must be a list")
    bodies, rejected, space = _plan_bodies(body, registry, findings)
    units = []
    for body_index, req in enumerate(bodies):
        count = _body_repetitions(req, f"planned body[{body_index}]")
        for repetition in range(count):
            units.append((req, repetition, 0))
    if len(units) > MAX_PLANNED_RUNS:
        raise PlanError(f"plan expands to {len(units)} runs, over the"
                        f" {MAX_PLANNED_RUNS} bound")
    report = _pre_execution_report(body, registry, bodies, units, rejected,
                                   space)
    return {"units": units, "report": report}


def _classify_config_error(message: str) -> str:
    lowered = message.lower()
    if any(token in lowered for token in _UNSUPPORTED_TOKENS):
        return "unsupported"
    return "failed"


def _planned_faults(body):
    faults = body.get("faults") if type(body) is dict else None
    if type(faults) is not list:
        return []
    cleaned = []
    for fault in faults:
        if type(fault) is not dict or type(fault.get("hook")) is not str:
            continue
        cleaned.append({
            "hook": fault["hook"],
            "required": fault.get("required") is True,
            "trigger": deepcopy(fault.get("trigger"))
            if type(fault.get("trigger")) is dict else None})
    return cleaned


def execute_plan(store, registry, plan, project_id: str, *,
                 policy_obj=None, requested_by: str = "t3-planner",
                 resolved_by: str = "t3-planner", findings=None) -> dict:
    """Execute every expanded unit through public ops; collect per-fault facts.

    Heldout plans are REFUSED before anything is touched. Per-fault outcomes
    are ``applied`` (fired), ``not-reached`` (never fired), ``unsupported``
    (the env cannot do it) or ``failed`` (malformed or errored). Required
    faults that never fired stay untested; rule violations and incomplete runs
    are preserved side by side, never collapsed into one status.
    """
    body = _check_plan(plan)
    if body["source"] == "heldout":
        raise PlanError(
            "refused: plan source is 'heldout' and no heldout owner"
            " interface exists; heldout execution is refused, nothing ran")
    policy = policy_obj if policy_obj is not None else body["policy"]
    if policy is None:
        raise PlanError("no policy: pass policy_obj or store one on the plan")
    if type(requested_by) is not str or not requested_by:
        raise PlanError("invalid requested_by")
    if type(resolved_by) is not str or not resolved_by:
        raise PlanError("invalid resolved_by")
    expanded = expand_plan(body, registry, findings=findings)
    units = expanded["units"]

    per_run: list = []
    per_fault: list = []
    rejected: list = list(expanded["report"]["rejected_proposals"])
    budgets: list = []
    tool_calls_total = 0

    groups: list = []
    for unit_index, (req, repetition, attempt) in enumerate(units):
        if groups and groups[-1][0] is req:
            groups[-1][1].append((unit_index, repetition, attempt))
        else:
            groups.append((req, [(unit_index, repetition, attempt)]))

    for req, members in groups:
        try:
            created = ops.create_requested_config(store, project_id, req,
                                                  policy, requested_by)
        except ops.ConfigError as error:
            reason = str(error)
            outcome = _classify_config_error(reason)
            for unit_index, repetition, attempt in members:
                rejected.append({"stage": "create",
                                 "unit_index": unit_index,
                                 "proposal_index": None,
                                 "reason": f"{reason}; rejected,"
                                           " never executed"})
                per_run.append({"unit_index": unit_index,
                                "repetition": repetition, "attempt": attempt,
                                "source": body["source"],
                                "outcome": "rejected", "stage": "create",
                                "reason": reason, "run_id": None,
                                "resolved_id": None, "status": None,
                                "verdict": None, "tool_calls": None,
                                "wall_clock_ms": None, "rules_failed": []})
                for fault in _planned_faults(req):
                    per_fault.append({"unit_index": unit_index,
                                      "target": fault["hook"],
                                      "required": fault["required"],
                                      "trigger": fault["trigger"],
                                      "outcome": outcome, "run_id": None})
            continue
        try:
            resolved = ops.resolve_config(store, registry, project_id,
                                          created["config_id"], policy,
                                          requested_by,
                                          resolved_by=resolved_by)
        except ops.ConfigError as error:
            reason = str(error)
            outcome = _classify_config_error(reason)
            for unit_index, repetition, attempt in members:
                rejected.append({"stage": "resolve",
                                 "unit_index": unit_index,
                                 "proposal_index": None,
                                 "reason": f"{reason}; rejected,"
                                           " never executed"})
                per_run.append({"unit_index": unit_index,
                                "repetition": repetition, "attempt": attempt,
                                "source": body["source"],
                                "outcome": "rejected", "stage": "resolve",
                                "reason": reason, "run_id": None,
                                "resolved_id": created["config_id"],
                                "status": None, "verdict": None,
                                "tool_calls": None, "wall_clock_ms": None,
                                "rules_failed": []})
                for fault in _planned_faults(req):
                    per_fault.append({"unit_index": unit_index,
                                      "target": fault["hook"],
                                      "required": fault["required"],
                                      "trigger": fault["trigger"],
                                      "outcome": outcome, "run_id": None})
            continue
        resolved_id = resolved["resolved_id"]
        for unit_index, repetition, attempt in members:
            try:
                started = ops.start_run(store, registry, project_id,
                                        resolved_id, repetition=repetition,
                                        attempt=attempt)
            except (ops.ConfigError, ops.BudgetExhaustedError) as error:
                reason = str(error)
                per_run.append({"unit_index": unit_index,
                                "repetition": repetition, "attempt": attempt,
                                "source": body["source"], "outcome": "failed",
                                "stage": "start", "reason": reason,
                                "run_id": None, "resolved_id": resolved_id,
                                "status": None, "verdict": None,
                                "tool_calls": None, "wall_clock_ms": None,
                                "rules_failed": []})
                for fault in _planned_faults(req):
                    per_fault.append({"unit_index": unit_index,
                                      "target": fault["hook"],
                                      "required": fault["required"],
                                      "trigger": fault["trigger"],
                                      "outcome": "failed", "run_id": None})
                continue
            record = ops.get_run(store, project_id, started["run_id"])
            run = record["run"]
            tool_calls_total += run["tool_calls"]
            rules_failed = [r for r in run.get("rules_evaluated", [])
                            if r in run.get("rules_failed", [])]
            per_run.append({"unit_index": unit_index, "repetition": repetition,
                            "attempt": attempt, "source": body["source"],
                            "outcome": "executed", "stage": None,
                            "reason": None, "run_id": started["run_id"],
                            "resolved_id": resolved_id,
                            "status": run["status"],
                            "verdict": run["verdict"],
                            "tool_calls": run["tool_calls"],
                            "wall_clock_ms": run["wall_clock_ms"],
                            "rules_failed": rules_failed})
            for fault in run.get("faults", []):
                if fault.get("fired") is True:
                    outcome = "applied"
                elif run["status"] == "error":
                    outcome = "failed"
                else:
                    outcome = "not-reached"
                per_fault.append({"unit_index": unit_index,
                                  "target": fault.get("hook"),
                                  "required": fault.get("required") is True,
                                  "trigger": deepcopy(fault.get("trigger"))
                                  if type(fault.get("trigger")) is dict
                                  else None,
                                  "outcome": outcome,
                                  "run_id": started["run_id"]})
        budgets.append(ops.budget_used(store, project_id, resolved_id))

    violations = [{"run_id": row["run_id"],
                   "rules_failed": list(row["rules_failed"])}
                  for row in per_run
                  if row["outcome"] == "executed" and row["rules_failed"]]
    incompletes = [{"run_id": row["run_id"], "status": row["status"]}
                   for row in per_run
                   if row["outcome"] == "executed"
                   and (row["status"] != "completed"
                        or row["verdict"] == "incomplete")]
    untested_required = [{"unit_index": row["unit_index"],
                          "target": row["target"], "trigger": row["trigger"],
                          "outcome": row["outcome"], "run_id": row["run_id"]}
                         for row in per_fault
                         if row["required"] and row["outcome"] != "applied"]
    all_tested = not untested_required
    runs_executed = sum(1 for row in per_run
                        if row["outcome"] == "executed")
    return {"plan_name": body["name"], "strategy": body["strategy"],
            "source": body["source"], "project_id": project_id,
            "planned_run_count": len(units),
            "planned_configs": len(groups), "runs_executed": runs_executed,
            "per_run": per_run, "per_fault": per_fault,
            "rejected": rejected, "violations": violations,
            "incompletes": incompletes,
            "untested_required": untested_required,
            "all_required_tested": all_tested,
            "passed": all_tested and not violations and not incompletes,
            "usage": {"tool_calls_total": tool_calls_total,
                      "budgets": budgets},
            "pre_execution": expanded["report"]}
