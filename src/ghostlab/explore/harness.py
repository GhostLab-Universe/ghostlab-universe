"""Bounded exploration harness (P4): fixed scenarios plus seeded random probing.

The harness drives worlds ONLY through the frozen P1 extension interfaces and
assesses every attempt with caller-supplied callbacks, so it stays decoupled
from any verifier. Three arms: fixed scenarios (explicit, reproducible),
seeded random (bounded stateful baseline over declared tools and faults) and
a scripted stand-in for future guided exploration. The guided arm executes a
fixed script -- no model, no network -- and its report marks it excluded from
effectiveness claims. Standard library only.
"""

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ghostlab.extensions.environment import Action, Observation, Outcome
from ghostlab.extensions.runner import run_attempt
from ghostlab.extensions.target import TargetInfo, TargetTask

FIXED_ARM = "fixed-scenario"
RANDOM_ARM = "seeded-random"
GUIDED_ARM = "guided"
GUIDED_MODE = "scripted-only"  # wiring only; excluded from effectiveness claims


class ScriptedTarget:
    """A deterministic target that proposes a fixed script, then finishes."""

    def __init__(self, actions, *, adapter_id="p4-scripted"):
        if type(adapter_id) is not str or not adapter_id:
            raise ValueError("Invalid adapter id")
        self._script = list(actions)
        self._adapter_id = adapter_id
        self._history: list[tuple[Action, Outcome]] = []
        self._started = False
        self.closed_with: str | None = None

    def info(self):
        return TargetInfo(
            adapter_id=self._adapter_id, adapter_version="p4-harness/1", model=None,
            provider=None, endpoint=None,
            actions=tuple({"tool": action.tool, "arguments": dict(action.arguments),
                           "status": outcome.status}
                          for action, outcome in self._history))

    def start(self, task, observation):
        if self._started:
            raise RuntimeError("Target is already started")
        if not isinstance(task, TargetTask) or not isinstance(observation, Observation):
            raise TypeError("Target start needs a TargetTask and an Observation")
        self._started = True

    def propose(self, observation):
        if not self._started or self.closed_with is not None:
            raise RuntimeError("Target is not running")
        if not isinstance(observation, Observation):
            raise TypeError("Target proposals need an Observation")
        if not self._script:
            return None
        action = self._script.pop(0)
        if not isinstance(action, Action):
            raise TypeError("Scripted actions must be Action")
        return action

    def notify_result(self, action, outcome):
        if not isinstance(action, Action) or not isinstance(outcome, Outcome):
            raise TypeError("Target results need an Action and an Outcome")
        self._history.append((action, outcome))

    def close(self, reason):
        if type(reason) is not str or not reason:
            raise ValueError("Invalid target close reason")
        if self.closed_with is None:
            self.closed_with = reason


class SamplingTarget:
    """A target that samples proposals from a seeded callable until it stops.

    ``sampler(rng, index, observation)`` returns the next Action or None to
    finish. The same seed always replays the same proposals.
    """

    def __init__(self, sampler, seed, *, adapter_id="p4-sampling",
                 max_proposals=100):
        if not callable(sampler):
            raise ValueError("Sampling needs a sampler callable")
        if type(seed) is not int:
            raise ValueError("Sampling needs an integer seed")
        if type(max_proposals) is not int or not 1 <= max_proposals <= 1000:
            raise ValueError("Invalid proposal bound")
        self._sampler = sampler
        self._rng = random.Random(seed)
        self._adapter_id = adapter_id
        self._max_proposals = max_proposals
        self._history: list[tuple[Action, Outcome]] = []
        self._index = 0
        self._started = False
        self.closed_with: str | None = None

    def info(self):
        return TargetInfo(
            adapter_id=self._adapter_id, adapter_version="p4-harness/1", model=None,
            provider=None, endpoint=None,
            actions=tuple({"tool": action.tool, "arguments": dict(action.arguments),
                           "status": outcome.status}
                          for action, outcome in self._history))

    def start(self, task, observation):
        if self._started:
            raise RuntimeError("Target is already started")
        if not isinstance(task, TargetTask) or not isinstance(observation, Observation):
            raise TypeError("Target start needs a TargetTask and an Observation")
        self._started = True

    def propose(self, observation):
        if not self._started or self.closed_with is not None:
            raise RuntimeError("Target is not running")
        if not isinstance(observation, Observation):
            raise TypeError("Target proposals need an Observation")
        if self._index >= self._max_proposals:
            return None
        action = self._sampler(self._rng, self._index, observation)
        self._index += 1
        if action is not None and not isinstance(action, Action):
            raise TypeError("Sampler proposals must be Action or None")
        return action

    def notify_result(self, action, outcome):
        if not isinstance(action, Action) or not isinstance(outcome, Outcome):
            raise TypeError("Target results need an Action and an Outcome")
        self._history.append((action, outcome))

    def close(self, reason):
        if type(reason) is not str or not reason:
            raise ValueError("Invalid target close reason")
        if self.closed_with is None:
            self.closed_with = reason

@dataclass(frozen=True)
class AttemptSpec:
    """One bounded attempt: world fixture, fault schedule, target and budget."""

    name: str
    arm: str
    fixture_name: str
    scenario: Mapping[str, Any]
    actor_id: str
    task: TargetTask
    target_factory: Callable[[], Any]
    cap: int

    def __post_init__(self):
        for value, label in ((self.name, "attempt name"),
                             (self.arm, "arm"),
                             (self.fixture_name, "fixture name"),
                             (self.actor_id, "actor id")):
            if type(value) is not str or not value or len(value) > 200:
                raise ValueError(f"Invalid {label}")
        if self.arm not in (FIXED_ARM, RANDOM_ARM, GUIDED_ARM):
            raise ValueError(f"Invalid arm {self.arm!r}")
        if not isinstance(self.scenario, Mapping):
            raise ValueError("Invalid scenario")
        if not isinstance(self.task, TargetTask):
            raise ValueError("Invalid task")
        if not callable(self.target_factory):
            raise ValueError("Invalid target factory")
        if type(self.cap) is not int or not 1 <= self.cap <= 1000:
            raise ValueError("Invalid action cap")


@dataclass(frozen=True)
class CampaignReport:
    """What a bounded campaign ran, covered and found."""

    attempts: tuple[Mapping[str, Any], ...]
    coverage: Mapping[str, Any]
    bounds: Mapping[str, Any]
    arms: Mapping[str, Any]

    def __post_init__(self):
        if not isinstance(self.attempts, tuple):
            raise ValueError("Invalid attempts")
        for mapping in (self.coverage, self.bounds, self.arms):
            if not isinstance(mapping, Mapping):
                raise ValueError("Invalid report section")


def _allow_all(action):
    return None


def run_campaign(specs, env_factory, *, authorize=None, assess=None,
                 max_attempts=100):
    """Run bounded attempts and assess each with the caller's callbacks.

    ``env_factory`` builds one fresh P1 environment per attempt; ``authorize``
    admits each proposal (default: allow; worlds still enforce their own
    checks); ``assess(trace, state)`` returns a plain-data finding summary.
    Every attempt resets from the named fixture, so attempts never share
    state. More specs than ``max_attempts`` is refused, not truncated.
    """
    specs = tuple(specs)
    if not specs or any(not isinstance(s, AttemptSpec) for s in specs):
        raise ValueError("run_campaign needs at least one AttemptSpec")
    if not callable(env_factory):
        raise ValueError("run_campaign needs an environment factory")
    if authorize is None:
        authorize = _allow_all
    if not callable(authorize):
        raise ValueError("authorize must be callable")
    if assess is not None and not callable(assess):
        raise ValueError("assess must be callable")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("Invalid attempt bound")
    if len(specs) > max_attempts:
        raise ValueError(f"{len(specs)} attempts exceed the bound of {max_attempts}")
    names = [s.name for s in specs]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate attempt name")

    attempts, tools_used, faults_fired, rules_failed = [], set(), set(), set()
    for spec in specs:
        env = env_factory()
        fixture = env.named_fixture(spec.fixture_name)
        env.reset(dict(fixture), dict(spec.scenario), actor_id=spec.actor_id)
        target = spec.target_factory()
        result = run_attempt(env, target, spec.task, authorize=authorize, cap=spec.cap)
        fired = env.faults_fired(dict(spec.scenario), list(result.trace))
        tools_used.update(event["request"]["tool"] for event in result.trace
                          if isinstance(event, Mapping))
        faults_fired.update(fired)
        findings = assess(list(result.trace), dict(result.state)) if assess else None
        if isinstance(findings, Mapping):
            rules_failed.update(findings.get("failed_rules", ()))
        attempts.append({"name": spec.name, "arm": spec.arm, "status": result.status,
                         "tool_calls": result.tool_calls, "denial": result.denial,
                         "stopped": result.stopped,
                         "faults_fired": tuple(sorted(fired)),
                         "trace": list(result.trace), "state": dict(result.state),
                         "findings": findings})
    arms = {arm: {"attempts": sum(1 for a in attempts if a["arm"] == arm)}
            for arm in (FIXED_ARM, RANDOM_ARM, GUIDED_ARM)}
    arms[GUIDED_ARM]["mode"] = GUIDED_MODE
    arms[GUIDED_ARM]["excluded_from_effectiveness"] = True
    return CampaignReport(
        attempts=tuple(attempts),
        coverage={"tools_used": tuple(sorted(tools_used)),
                  "faults_fired": tuple(sorted(faults_fired)),
                  "rules_failed": tuple(sorted(rules_failed))},
        bounds={"max_attempts": max_attempts, "attempts_run": len(attempts),
                "action_caps": tuple(s.cap for s in specs),
                "caps_respected": all(a["tool_calls"] <= s.cap
                                      for a, s in zip(attempts, specs))},
        arms=arms)


def refund_action_sampler(order_id, customer_id, paid_minor, *, key_pool=4,
                          length=(1, 8)):
    """Seeded sampler over valid refund calls; small key pool forces collisions.

    Every proposal is schema-valid for the caller-bound customer; variation
    comes from the tool mix, the amounts and idempotency-key reuse. ``length``
    bounds the proposals per attempt. Deterministic per seed.
    """
    if key_pool < 1 or length[0] < 0 or length[1] < length[0]:
        raise ValueError("Invalid sampler bounds")
    plan: dict[int, int] = {}

    def sampler(rng, index, observation):
        if id(rng) not in plan:
            plan[id(rng)] = rng.randint(length[0], length[1])
        if index >= plan[id(rng)]:
            return None
        identity = {"order_id": order_id, "customer_id": customer_id}
        choice = rng.random()
        if choice < 0.25:
            return Action(tool="get_order", arguments=dict(identity))
        key = f"sample-key-{rng.randint(1, key_pool)}"
        if choice < 0.5:
            return Action(tool="get_refund_status",
                           arguments={**identity, "idempotency_key": key})
        amount = paid_minor if rng.random() < 0.5 else rng.randint(1, paid_minor)
        return Action(tool="issue_refund",
                       arguments={**identity, "amount_minor": amount,
                                  "idempotency_key": key})

    return sampler


def refund_scenarios():
    """Fixed refund fault schedules: clean plus timeout-after-commit."""
    return ({"timeout_after_commit": False, "max_tool_calls": 20},
            {"timeout_after_commit": True, "max_tool_calls": 20})


def run_scripted_guided(specs, env_factory, *, authorize=None, assess=None,
                        max_attempts=100):
    """SCRIPTED-ONLY guided arm: wiring for a future guided explorer.

    Executes the given fixed-script specs through the normal campaign path so
    the guided arm's plumbing (specs, assessment, report shape) is exercised
    today. Makes no model call and takes no guidance input; the report marks
    the arm ``excluded_from_effectiveness`` and every spec must use the guided
    arm label, or the run is refused.
    """
    specs = tuple(specs)
    if any(s.arm != GUIDED_ARM for s in specs):
        raise ValueError("scripted guided runs take guided-arm specs only")
    return run_campaign(specs, env_factory, authorize=authorize, assess=assess,
                        max_attempts=max_attempts)
