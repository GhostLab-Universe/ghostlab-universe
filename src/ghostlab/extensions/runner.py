"""Generic target-vs-environment runner (``ghostlab-extensions/1``).

:func:`run_attempt` owns only the loop: ask the target, authorize through the
caller-injected check, dispatch, report back. It contains no world logic,
assumes no optional capabilities, and records nothing except what the
environment observed. Policy, budgets and verdicts stay with the caller:
``authorize`` decides each action, ``cap`` bounds the attempt, and the
returned evidence is assembled into run records by the world binding.
Standard library only.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .environment import (Action, ActionBudgetExceeded, EnvironmentAdapter, Observation,
                          Outcome)
from .registry import check_environment, check_target
from .target import BUDGET_EXHAUSTED, DENIED, ERROR, FINISHED, TargetAdapter, TargetTask


@dataclass(frozen=True)
class AttemptResult:
    """Evidence of one attempt: the trace, the state and how the loop ended.

    ``denial`` carries the opaque decision ``authorize`` returned;
    ``stopped`` carries the controller's stop reason. Either one takes
    precedence over ``status``: the caller discards the partial run.
    """

    trace: list
    state: Mapping[str, Any]
    status: str
    tool_calls: int
    denial: Any | None = None
    stopped: str | None = None

    def __post_init__(self):
        if type(self.trace) is not list or len(self.trace) > 1000:
            raise ValueError("Invalid attempt trace")
        if not isinstance(self.state, Mapping):
            raise ValueError("Invalid attempt state")
        if self.status not in ("completed", "budget_exhausted"):
            raise ValueError(f"Invalid attempt status {self.status!r}")
        if type(self.tool_calls) is not int or self.tool_calls < 0:
            raise ValueError("Invalid attempt tool count")
        if self.stopped is not None and (type(self.stopped) is not str or not self.stopped):
            raise ValueError("Invalid attempt stop reason")


def run_attempt(env: EnvironmentAdapter, target: TargetAdapter, task: TargetTask, *,
                authorize: Callable[[Action], Any | None], cap: int,
                should_stop: Callable[[], str | None] | None = None,
                cooperative_stop: bool = True) -> AttemptResult:
    """Run one target against a reset environment and return its evidence.

    ``authorize`` decides each proposed action before dispatch and returns
    None when allowed or an opaque denial otherwise; a denial ends the attempt
    without touching the world. ``cap`` bounds applied actions: proposing
    past it ends the attempt as ``budget_exhausted``. When
    ``cooperative_stop`` holds, ``should_stop`` is consulted before every
    proposal and a non-None reason ends the attempt as stopped. The runner
    always closes the target (with the ending reason) and the environment.
    Target and environment failures propagate after closing; the environment
    stays authoritative over what was recorded.
    """
    check_environment(env)
    check_target(target)
    if not isinstance(task, TargetTask):
        raise TypeError("run_attempt task must be a TargetTask")
    if not callable(authorize):
        raise TypeError("run_attempt authorize must be callable")
    if type(cap) is not int or not 1 <= cap <= 1000:
        raise ValueError("cap must be an integer from 1 to 1000")
    if should_stop is not None and not callable(should_stop):
        raise TypeError("run_attempt should_stop must be callable")
    stop = should_stop if should_stop is not None else lambda: None

    def finished(reason: str, status: str, calls: int, denial=None, stopped=None):
        target.close(reason)
        observed = env.observe()
        return AttemptResult(list(observed.trace), observed.state, status, calls, denial,
                             stopped)

    try:
        target.start(task, env.observe())
        calls = 0
        while True:
            if cooperative_stop:
                reason = stop()
                if reason is not None:
                    if type(reason) is not str or not reason:
                        raise ValueError("Invalid stop reason")
                    return finished(reason, "completed", calls, stopped=reason)
            action = target.propose(env.observe())
            if action is None:
                return finished(FINISHED, "completed", calls)
            if not isinstance(action, Action):
                raise TypeError("Target proposals must be Action or None")
            denial = authorize(action)
            if denial is not None:
                return finished(DENIED, "completed", calls, denial=denial)
            if calls >= cap:
                return finished(BUDGET_EXHAUSTED, "budget_exhausted", calls)
            try:
                outcome = env.execute(action)
            except ActionBudgetExceeded:
                return finished(BUDGET_EXHAUSTED, "budget_exhausted", calls)
            if not isinstance(outcome, Outcome):
                raise TypeError("Environment outcomes must be Outcome")
            calls += 1
            target.notify_result(action, outcome)
    except Exception:
        try:
            target.close(ERROR)
        except Exception:
            pass
        raise
    finally:
        env.close()


def first_divergence(baseline_trace: list, faulted_trace: list) -> dict | None:
    """First position where two recorded traces differ, or None when equal.

    Compares recorded events in order; a longer trace diverges where the
    shorter one ends. Pure and world-agnostic: diff assembly adds the world's
    own metrics and violated invariants around it.
    """
    for trace, label in ((baseline_trace, "baseline"), (faulted_trace, "faulted")):
        if type(trace) is not list or len(trace) > 1000:
            raise ValueError(f"Invalid {label} trace")
    for index in range(max(len(baseline_trace), len(faulted_trace))):
        left = baseline_trace[index] if index < len(baseline_trace) else None
        right = faulted_trace[index] if index < len(faulted_trace) else None
        if left != right:
            return {"event_id": index + 1, "baseline": left, "faulted": right}
    return None


def replay_trace(env: EnvironmentAdapter, recorded_trace: list) -> Observation:
    """Re-execute recorded requests against a reset environment.

    The environment must already be reset to the recorded fixture, scenario
    and actor. Each recorded request executes in order; world errors are
    recorded outcomes, not failures, exactly as during the run. A budget
    refusal ends the replay: the shortened trace then mismatches the
    recording. No authorization check runs here -- replay re-executes trusted
    recorded evidence, it does not admit new proposals.
    """
    check_environment(env)
    if type(recorded_trace) is not list or len(recorded_trace) > 1000:
        raise ValueError("Invalid recorded trace")
    for event in recorded_trace:
        if not isinstance(event, Mapping):
            raise ValueError("Invalid recorded event")
        request = event.get("request")
        if not isinstance(request, Mapping):
            raise ValueError("Invalid recorded event")
        try:
            action = Action(tool=request["tool"], arguments=request["arguments"])
        except (KeyError, ValueError):
            raise ValueError("Invalid recorded tool request") from None
        try:
            env.execute(action)
        except ActionBudgetExceeded:
            break
    return env.observe()