"""Greedy shortest-prefix minimization over recorded action traces.

Given recorded ``[{"tool", "arguments"}]`` actions whose replay violates an
invariant, :func:`minimize_prefix` finds the shortest leading prefix whose
replay still violates it, so a regression guard can replay a small,
fast, stable artifact instead of the full trace.

Adapter protocol (generic; no model calls, no secrets):

- ``reset(fixture_dict)`` restores the initial state,
- ``invoke(tool, arguments)`` dispatches one action (tool errors that the
  world records, i.e. ``ghostlab.core.ToolError`` such as the expected
  ``PaymentTimeout``, are tolerated mid-replay and replay continues),
- ``evidence()`` returns ``{"snapshot", "trace"}`` after the replay.

Preconditions:

- The full recorded trace must still violate on replay; otherwise there is
  nothing to minimize and :func:`minimize_prefix` raises ``ValueError``.
- The adapter must be deterministic: the same fixture plus the same prefix
  must reproduce equal evidence. The chosen prefix is replayed twice and the
  result reports ``stable`` (both replays violate with identical evidence).
  A nondeterministic adapter yields ``stable=False``; such a prefix must not
  back a regression guard.
"""

from copy import deepcopy

from ..core import Fixture, ToolError, verify


def replay_actions(adapter, fixture: dict, actions: list) -> dict:
    """Reset and replay actions verbatim; return the resulting evidence.

    Each action is ``{"tool", "arguments"}``. Recorded world outcomes that
    raise ``ToolError`` (including the expected timeout-after-commit) are
    part of the trace, so replay continues past them; anything else
    propagates. Neither the fixture nor the actions are mutated.
    """
    if type(fixture) is not dict:
        raise ValueError("fixture must be an object")
    checked = check_actions(actions)
    adapter.reset(deepcopy(fixture))
    for action in checked:
        try:
            adapter.invoke(action["tool"], deepcopy(action["arguments"]))
        except ToolError:
            continue
    return adapter.evidence()


def check_actions(actions) -> list:
    """Validate recorded actions; return a detached plain-data copy."""
    if type(actions) is not list or not actions:
        raise ValueError("actions must be a non-empty list")
    checked = []
    for index, action in enumerate(actions):
        if type(action) is not dict or set(action) != {"tool", "arguments"}:
            raise ValueError(f"actions[{index}] must carry exactly"
                             " tool and arguments")
        if type(action["tool"]) is not str or not action["tool"]:
            raise ValueError(f"invalid actions[{index}].tool")
        if type(action["arguments"]) is not dict:
            raise ValueError(f"invalid actions[{index}].arguments")
        checked.append({"tool": action["tool"],
                        "arguments": deepcopy(action["arguments"])})
    return checked


def refund_rules_violated(evidence) -> bool:
    """Whether replay evidence violates any v1 refund invariant.

    Takes the ``adapter.evidence()`` dict; the fixture is recovered from the
    snapshot's embedded order, so guard files need no extra wiring.
    """
    if type(evidence) is not dict:
        raise ValueError("evidence must be an object")
    snapshot = evidence.get("snapshot")
    if type(snapshot) is not dict or type(snapshot.get("order")) is not dict:
        raise ValueError("evidence snapshot must embed the order")
    checks = verify(Fixture(**snapshot["order"]), snapshot)
    return not all(checks.values())


def minimize_prefix(new_adapter, fixture: dict, actions: list,
                    is_violation) -> dict:
    """Find the shortest violating prefix of recorded actions.

    ``new_adapter`` is a zero-argument factory returning a fresh adapter per
    replay; ``is_violation`` maps replay evidence to bool. Prefixes are tried
    shortest-first (1..N actions); the first prefix whose replay still
    violates is replayed twice more to confirm determinism. Returns
    ``{"required_prefix", "dropped", "stable", "replays": 2}`` where
    ``required_prefix`` is the minimal violating prefix, ``dropped`` counts
    the trimmed trailing actions, and ``stable`` is true only when both
    confirmatory replays violate with identical evidence.
    """
    if not callable(new_adapter):
        raise ValueError("new_adapter must be a zero-argument factory")
    if not callable(is_violation):
        raise ValueError("is_violation must be a callable")
    checked = check_actions(actions)
    if type(fixture) is not dict:
        raise ValueError("fixture must be an object")
    full = replay_actions(new_adapter(), fixture, checked)
    if not is_violation(full):
        raise ValueError("the full recorded trace does not violate on"
                         " replay; there is nothing to minimize")
    required = list(checked)
    for width in range(1, len(checked) + 1):
        candidate = checked[:width]
        if is_violation(replay_actions(new_adapter(), fixture, candidate)):
            required = candidate
            break
    first = replay_actions(new_adapter(), fixture, required)
    second = replay_actions(new_adapter(), fixture, required)
    stable = (is_violation(first) and is_violation(second)
              and first == second)
    return {"required_prefix": deepcopy(required),
            "dropped": len(checked) - len(required),
            "stable": bool(stable), "replays": 2}
