"""Divergence harness: replay identical actions against two adapters.

Fidelity means (a) which faults the real service could exhibit and the
checked-in world models, and (b) whether the world's state transitions match
the real service's transitions for the declared tools.

``compare_runs`` checks (b) behaviorally: both adapters reset from the same
fixture, replay the same actions step by step, and snapshots plus traces are
compared per step. Tool/schema mismatches make the pair incomparable with a
recorded reason instead of raising. ``fidelity_probe`` checks one adapter's
double-replay determinism and returns the ordered state-transition log.
Standard library only; no model calls.
"""

import hashlib
import json
from copy import deepcopy

MAX_DIFFS = 50

# Dispatch rejections that mean the action sequence is not valid for an
# adapter (unknown tool or wrong shape), as opposed to a modeled runtime
# outcome such as PaymentTimeout. A mismatch on these is incomparability,
# never a divergence and never a crash.
_INCOMPARABLE_MESSAGES = frozenset({
    "Unknown tool",
    "Arguments do not match tool schema",
})


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False, default=str)


def _digest(obj) -> str:
    return hashlib.sha256(_canonical(obj).encode()).hexdigest()


def _short(obj, limit: int = 200) -> str:
    text = _canonical(obj)
    return text if len(text) <= limit else text[:limit] + "..."


def _check_actions(actions) -> list:
    if type(actions) is not list:
        raise ValueError("actions must be a list")
    checked = []
    for index, action in enumerate(actions):
        if (type(action) is not dict or set(action) != {"tool", "arguments"}
                or type(action["tool"]) is not str or not action["tool"]
                or type(action["arguments"]) is not dict):
            raise ValueError(f"actions[{index}] must carry tool and arguments")
        checked.append({"tool": action["tool"],
                        "arguments": deepcopy(action["arguments"])})
    return checked


def _capture_invoke(adapter, tool: str, arguments: dict) -> dict:
    try:
        result = adapter.invoke(tool, deepcopy(arguments))
    except Exception as error:  # noqa: BLE001 - outcomes are data here
        return {"ok": False, "error": type(error).__name__, "message": str(error)}
    return {"ok": True, "result": deepcopy(result)}


def _is_tool_schema_error(outcome: dict) -> bool:
    return (not outcome["ok"]
            and outcome.get("message") in _INCOMPARABLE_MESSAGES)


def _describe_tools(adapter) -> dict:
    description = adapter.describe()
    tools = description["tools"]
    if type(tools) is not list:
        raise ValueError("describe() tools must be a list")
    mapping = {}
    for entry in tools:
        mapping[entry["name"]] = list(entry["parameters"])
    return mapping


def _incomparable(reason: str) -> dict:
    return {"comparable": False, "reason": reason, "diverged": False,
            "first_divergence_step": None, "diffs": []}


def compare_runs(new_a, new_b, fixture: dict, actions: list) -> dict:
    """Replay identical actions on two fresh adapters and compare per step.

    ``new_a``/``new_b`` are zero-argument adapter factories. Returns
    ``{"comparable", "reason", "diverged", "first_divergence_step", "diffs"}``
    with ``diffs`` capped at 50 entries. Tool/schema mismatches (including
    differing tool sets or fixtures one side rejects) yield
    ``comparable=False`` with a reason instead of raising.
    """
    if not callable(new_a) or not callable(new_b):
        raise ValueError("compare_runs needs two adapter factories")
    if type(fixture) is not dict:
        raise ValueError("fixture must be an object")
    checked_actions = _check_actions(actions)
    try:
        adapter_a = new_a()
        adapter_a.reset(deepcopy(fixture))
    except Exception as error:  # noqa: BLE001 - reported as incomparability
        return _incomparable(f"side-a reset failed: {type(error).__name__}: {error}")
    try:
        adapter_b = new_b()
        adapter_b.reset(deepcopy(fixture))
    except Exception as error:  # noqa: BLE001 - reported as incomparability
        return _incomparable(f"side-b reset failed: {type(error).__name__}: {error}")
    try:
        tools_a = _describe_tools(adapter_a)
        tools_b = _describe_tools(adapter_b)
    except Exception as error:  # noqa: BLE001 - reported as incomparability
        return _incomparable(f"describe failed: {type(error).__name__}: {error}")
    if tools_a != tools_b:
        names_a = sorted(tools_a)
        names_b = sorted(tools_b)
        return _incomparable(
            f"tool sets differ: a={names_a} b={names_b}")
    diffs: list = []
    for step, action in enumerate(checked_actions, start=1):
        tool = action["tool"]
        outcome_a = _capture_invoke(adapter_a, tool, action["arguments"])
        outcome_b = _capture_invoke(adapter_b, tool, action["arguments"])
        if ((_is_tool_schema_error(outcome_a)
                or _is_tool_schema_error(outcome_b))
                and outcome_a != outcome_b):
            return _incomparable(
                f"step {step}: tool/schema mismatch for '{tool}': "
                f"a={_short(outcome_a)} b={_short(outcome_b)}")
        try:
            snapshot_a = adapter_a.snapshot()
            snapshot_b = adapter_b.snapshot()
        except Exception as error:  # noqa: BLE001 - reported, never raised
            return _incomparable(
                f"step {step}: snapshot failed: {type(error).__name__}: {error}")
        try:
            trace_a = adapter_a.evidence()["trace"]
            trace_b = adapter_b.evidence()["trace"]
        except Exception as error:  # noqa: BLE001 - reported, never raised
            return _incomparable(
                f"step {step}: evidence failed: {type(error).__name__}: {error}")
        step_diffs = []
        if outcome_a != outcome_b:
            step_diffs.append(
                {"step": step, "kind": "outcome", "action": tool,
                 "detail": f"a={_short(outcome_a)} b={_short(outcome_b)}"})
        if snapshot_a != snapshot_b:
            step_diffs.append(
                {"step": step, "kind": "snapshot", "action": tool,
                 "detail": f"snapshot digests differ: "
                           f"a={_digest(snapshot_a)[:12]} "
                           f"b={_digest(snapshot_b)[:12]}"})
        if trace_a != trace_b:
            step_diffs.append(
                {"step": step, "kind": "trace", "action": tool,
                 "detail": f"trace differs: lengths a={len(trace_a)} "
                           f"b={len(trace_b)} digests a={_digest(trace_a)[:12]} "
                           f"b={_digest(trace_b)[:12]}"})
        for entry in step_diffs:
            if len(diffs) < MAX_DIFFS:
                diffs.append(entry)
    first = diffs[0]["step"] if diffs else None
    return {"comparable": True, "reason": None, "diverged": bool(diffs),
            "first_divergence_step": first, "diffs": diffs}


def _replay(new_adapter, fixture: dict, checked_actions: list) -> list:
    adapter = new_adapter()
    adapter.reset(deepcopy(fixture))
    transitions = []
    for step, action in enumerate(checked_actions, start=1):
        outcome = _capture_invoke(adapter, action["tool"], action["arguments"])
        snapshot = adapter.snapshot()
        if outcome["ok"]:
            label = "ok"
        else:
            label = f"error:{outcome['error']}"
        transitions.append({"step": step, "tool": action["tool"],
                            "outcome": label,
                            "snapshot_digest": _digest(snapshot),
                            "snapshot": snapshot, "captured": outcome})
    return transitions


def fidelity_probe(new_adapter, fixture: dict, actions: list) -> dict:
    """Double-replay one adapter and return determinism plus transitions.

    Returns ``{"deterministic", "replays", "steps",
    "first_nondeterminism_step", "transitions", "final_snapshot_digest"}``
    where ``transitions`` is the ordered per-step state-transition log of the
    first replay. Fixture/adapter-construction errors raise; per-action tool
    errors are recorded as transition outcomes.
    """
    if not callable(new_adapter):
        raise ValueError("fidelity_probe needs an adapter factory")
    if type(fixture) is not dict:
        raise ValueError("fixture must be an object")
    checked_actions = _check_actions(actions)
    first = _replay(new_adapter, fixture, checked_actions)
    second = _replay(new_adapter, fixture, checked_actions)
    bad_step = None
    for left, right in zip(first, second):
        if (left["captured"] != right["captured"]
                or left["snapshot"] != right["snapshot"]):
            bad_step = left["step"]
            break
    transitions = [{key: entry[key] for key in
                    ("step", "tool", "outcome", "snapshot_digest")}
                   for entry in first]
    final_digest = first[-1]["snapshot_digest"] if first else _digest({})
    return {"deterministic": bad_step is None, "replays": 2,
            "steps": len(checked_actions),
            "first_nondeterminism_step": bad_step,
            "transitions": transitions,
            "final_snapshot_digest": final_digest}
