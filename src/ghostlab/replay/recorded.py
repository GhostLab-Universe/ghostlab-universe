"""Recorded-action replay and fresh-agent retest for extension environments.

Generic over ``ghostlab-extensions/1``: no world logic, no model calls, no
network. :func:`replay_recorded` re-executes trusted recorded evidence
against a freshly reset environment; :func:`retest_fresh` runs a (possibly
changed) target as a new experiment. A retest never claims replay
equivalence with the recorded run: the result carries that refusal
explicitly (``replay_equivalence_claimed`` is always False).
"""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ghostlab.extensions.environment import Action, InvariantCheck
from ghostlab.extensions.registry import AdapterRegistry
from ghostlab.extensions.runner import first_divergence, replay_trace, run_attempt
from ghostlab.extensions.target import TargetTask

REPLAY_SCHEMA = "ghostlab-recorded-replay/1"
RETEST_SCHEMA = "ghostlab-fresh-retest/1"
STATUSES = ("completed", "budget_exhausted")


def canonical(value: Any) -> bytes:
    """Canonical JSON bytes (same bytes as diagnostics minimization)."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode()
    except (TypeError, ValueError) as error:
        raise ValueError("Evidence is not canonical JSON") from error


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(value[key]) for key in value}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def normalize_check(check: Any) -> dict:
    """One invariant verdict as plain data, strictly validated."""
    if isinstance(check, InvariantCheck):
        return {"name": check.name, "passed": check.passed,
                "evidence_event_ids": list(check.evidence_event_ids),
                "observed": check.observed, "limit": check.limit, "unit": check.unit}
    if not isinstance(check, Mapping) or set(check) != {
            "name", "passed", "evidence_event_ids", "observed", "limit", "unit"}:
        raise ValueError("Invalid invariant check")
    try:
        built = InvariantCheck(
            name=check["name"], passed=check["passed"],
            evidence_event_ids=tuple(check["evidence_event_ids"]),
            observed=check["observed"], limit=check["limit"], unit=check["unit"])
    except (TypeError, ValueError):
        raise ValueError("Invalid invariant check") from None
    return normalize_check(built)


def check_verdicts(checks: Any) -> dict:
    """Invariant name to exact-boolean verdict, strictly validated."""
    if not isinstance(checks, (list, tuple)) or not checks:
        raise ValueError("Invalid invariant checks")
    rows = [normalize_check(check) for check in checks]
    names = [row["name"] for row in rows]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate invariant name")
    return {row["name"]: row["passed"] for row in rows}


def failure_witness(checks: Any) -> dict:
    """Failed invariants plus their cited evidence, sorted.

    Logical event positions may move; the failed names and their evidence
    may not. A reduction that changes any of it demonstrates a different
    failure, even under the same invariant name.
    """
    if not isinstance(checks, (list, tuple)) or not checks:
        raise ValueError("Invalid invariant checks")
    failed = [row for row in (normalize_check(check) for check in checks)
              if row["passed"] is False]
    failed.sort(key=canonical)
    return {"failed": failed}


def witness_sha256(witness: Any) -> str:
    """Canonical digest of a failure witness."""
    return hashlib.sha256(canonical(witness)).hexdigest()


def refuse_unless_recorded(*, replay_kind: str, truncated: bool,
                           external_names: Any) -> None:
    """Refuse recorded replay for anything but plain recorded evidence."""
    if type(replay_kind) is not str:
        raise ValueError("Invalid replay kind")
    if type(truncated) is not bool:
        raise ValueError("Invalid truncation flag")
    if not isinstance(external_names, (list, tuple)) or any(
            type(name) is not str for name in external_names):
        raise ValueError("Invalid external dependency names")
    if replay_kind != "recorded":
        raise ValueError("retest capsules are new experiments; "
                         "rerun the target instead of recorded replay")
    if truncated:
        raise ValueError("truncated capsules carry incomplete evidence; "
                         "a replay would compare against a cut-short trace")
    if external_names:
        names = ", ".join(sorted(external_names))
        raise ValueError(f"capsule needs resources outside itself: {names}")


def allow_tools(*names: str) -> Callable[[Action], str | None]:
    """An authorize callable allowing exactly the named tools.

    Names only: argument schemas and identity stay with the environment's
    own validation at the commitment boundary.
    """
    for name in names:
        if type(name) is not str or not name:
            raise ValueError("Invalid allowed tool name")
    allowed = frozenset(names)

    def authorize(action: Action) -> str | None:
        if not isinstance(action, Action):
            raise TypeError("authorize sees Action proposals only")
        if action.tool in allowed:
            return None
        return f"tool_not_allowed:{action.tool}"

    return authorize


@dataclass(frozen=True)
class ReplayResult:
    """Recorded-action replay against a freshly reset environment."""

    schema: str
    world_version: str
    reproduced: bool
    matches: dict
    divergence: dict | None
    tool_calls: int
    checks: dict
    failed: tuple
    witness: dict
    witness_sha256: str
    model_calls: int = 0

    def __post_init__(self):
        if self.schema != REPLAY_SCHEMA:
            raise ValueError("Invalid replay schema")
        if type(self.world_version) is not str or not self.world_version:
            raise ValueError("Invalid replay world version")
        if type(self.reproduced) is not bool:
            raise ValueError("Invalid replay verdict")
        if (type(self.matches) is not dict
                or set(self.matches) != {"trace", "final_state", "checks"}
                or any(type(hit) is not bool for hit in self.matches.values())):
            raise ValueError("Invalid replay matches")
        if self.reproduced != all(self.matches.values()):
            raise ValueError("Replay verdict does not match its comparisons")
        if self.divergence is not None and (
                type(self.divergence) is not dict
                or set(self.divergence) != {"event_id", "baseline", "faulted"}):
            raise ValueError("Invalid replay divergence")
        if type(self.tool_calls) is not int or not 0 <= self.tool_calls <= 1000:
            raise ValueError("Invalid replay tool count")
        if (type(self.checks) is not dict
                or any(type(name) is not str or type(hit) is not bool
                       for name, hit in self.checks.items())):
            raise ValueError("Invalid replay checks")
        object.__setattr__(self, "failed", tuple(self.failed))
        if (any(type(name) is not str for name in self.failed)
                or set(self.failed) != {name for name, hit in self.checks.items()
                                        if hit is False}):
            raise ValueError("Invalid replay failures")
        if type(self.witness) is not dict or set(self.witness) != {"failed"}:
            raise ValueError("Invalid replay witness")
        if (type(self.witness_sha256) is not str
                or witness_sha256(self.witness) != self.witness_sha256):
            raise ValueError("Invalid replay witness digest")
        if self.model_calls != 0:
            raise ValueError("Recorded replay makes no model call")

    def to_dict(self) -> dict:
        return {"schema": self.schema, "world_version": self.world_version,
                "reproduced": self.reproduced, "matches": dict(self.matches),
                "divergence": self.divergence, "tool_calls": self.tool_calls,
                "checks": dict(self.checks), "failed": list(self.failed),
                "witness": self.witness, "witness_sha256": self.witness_sha256,
                "model_calls": self.model_calls}


def replay_recorded(*, registry: AdapterRegistry, world_version: str, fixture: Mapping,
                    scenario: Mapping, actor_id: str, recorded_trace: list,
                    recorded_state: Mapping, recorded_checks: Mapping) -> ReplayResult:
    """Re-execute recorded requests on a fresh instance and compare evidence.

    Unknown worlds fail closed at resolve time. Fixture or scenario shapes
    the environment rejects are caller errors, not mismatches: the recorded
    evidence claims they ran.
    """
    if not isinstance(registry, AdapterRegistry):
        raise TypeError("replay_recorded registry must be an AdapterRegistry")
    if type(world_version) is not str or not world_version:
        raise ValueError("Invalid world version")
    if not isinstance(fixture, Mapping) or not isinstance(scenario, Mapping):
        raise ValueError("Invalid fixture or scenario")
    if type(actor_id) is not str or not actor_id:
        raise ValueError("Invalid actor id")
    if type(recorded_trace) is not list or len(recorded_trace) > 1000:
        raise ValueError("Invalid recorded trace")
    if not isinstance(recorded_state, Mapping):
        raise ValueError("Invalid recorded state")
    if (type(recorded_checks) is not dict
            or any(type(name) is not str or type(hit) is not bool
                   for name, hit in recorded_checks.items())):
        raise ValueError("Invalid recorded checks")
    recorded_trace = _plain(recorded_trace)
    recorded_state = _plain(recorded_state)
    env = registry.environment_factory(world_version)()
    try:
        env.reset(_plain(fixture), _plain(scenario), actor_id=actor_id)
        replay_trace(env, recorded_trace)
        observed = env.observe()
        checks = env.evaluate(trace=observed.trace, state=observed.state)
    finally:
        env.close()
    trace = _plain(observed.trace)
    state = _plain(observed.state)
    matches = {"trace": canonical(trace) == canonical(recorded_trace),
               "final_state": canonical(state) == canonical(recorded_state),
               "checks": check_verdicts(checks) == recorded_checks}
    verdicts = check_verdicts(checks)
    witness = failure_witness(checks)
    return ReplayResult(
        schema=REPLAY_SCHEMA, world_version=world_version,
        reproduced=all(matches.values()), matches=matches,
        divergence=None if matches["trace"] else first_divergence(recorded_trace, trace),
        tool_calls=len(trace), checks=verdicts,
        failed=tuple(sorted(name for name, hit in verdicts.items() if hit is False)),
        witness=witness, witness_sha256=witness_sha256(witness))


@dataclass(frozen=True)
class RetestResult:
    """A fresh target run under the same scenario: a new experiment.

    ``replay_equivalence_claimed`` is always False: fresh inference or a
    changed script may take a different trajectory, so a retest can pass or
    fail but can never claim to be the recorded run.
    """

    schema: str
    world_version: str
    target_adapter_id: str
    target_adapter_version: str
    model: str | None
    provider: str | None
    endpoint: str | None
    status: str
    tool_calls: int
    stopped: str | None
    denied: bool
    denial_summary: str | None
    checks: dict
    failed: tuple
    trace: list
    final_state: dict
    witness: dict
    witness_sha256: str
    replay_equivalence_claimed: bool = False
    model_calls: int | None = None

    def __post_init__(self):
        if self.schema != RETEST_SCHEMA:
            raise ValueError("Invalid retest schema")
        for value, label in ((self.world_version, "world version"),
                             (self.target_adapter_id, "target adapter id"),
                             (self.target_adapter_version, "target adapter version")):
            if type(value) is not str or not value:
                raise ValueError(f"Invalid retest {label}")
        for value, label in ((self.model, "model"), (self.provider, "provider"),
                             (self.endpoint, "endpoint")):
            if value is not None and (type(value) is not str or not value):
                raise ValueError(f"Invalid retest {label}")
        if self.status not in STATUSES:
            raise ValueError("Invalid retest status")
        if type(self.tool_calls) is not int or not 0 <= self.tool_calls <= 1000:
            raise ValueError("Invalid retest tool count")
        if self.stopped is not None and (type(self.stopped) is not str
                                         or not self.stopped):
            raise ValueError("Invalid retest stop reason")
        if type(self.denied) is not bool:
            raise ValueError("Invalid retest denial flag")
        if self.denied == (self.denial_summary is None):
            raise ValueError("A denied retest names its denial")
        if (type(self.checks) is not dict
                or any(type(name) is not str or type(hit) is not bool
                       for name, hit in self.checks.items())):
            raise ValueError("Invalid retest checks")
        object.__setattr__(self, "failed", tuple(self.failed))
        if (any(type(name) is not str for name in self.failed)
                or set(self.failed) != {name for name, hit in self.checks.items()
                                        if hit is False}):
            raise ValueError("Invalid retest failures")
        if type(self.trace) is not list or len(self.trace) > 1000:
            raise ValueError("Invalid retest trace")
        if type(self.final_state) is not dict:
            raise ValueError("Invalid retest state")
        if type(self.witness) is not dict or set(self.witness) != {"failed"}:
            raise ValueError("Invalid retest witness")
        if (type(self.witness_sha256) is not str
                or witness_sha256(self.witness) != self.witness_sha256):
            raise ValueError("Invalid retest witness digest")
        if self.replay_equivalence_claimed is not False:
            raise ValueError("A retest never claims replay equivalence")
        if self.model_calls is not None:
            raise ValueError("External targets do not report inference usage here; "
                             "count it at the harness that spent it")

    def to_dict(self) -> dict:
        return {"schema": self.schema, "world_version": self.world_version,
                "target_adapter_id": self.target_adapter_id,
                "target_adapter_version": self.target_adapter_version,
                "model": self.model, "provider": self.provider, "endpoint": self.endpoint,
                "status": self.status, "tool_calls": self.tool_calls,
                "stopped": self.stopped, "denied": self.denied,
                "denial_summary": self.denial_summary, "checks": dict(self.checks),
                "failed": list(self.failed), "trace": self.trace,
                "final_state": self.final_state, "witness": self.witness,
                "witness_sha256": self.witness_sha256,
                "replay_equivalence_claimed": self.replay_equivalence_claimed,
                "model_calls": self.model_calls,
                "model_calls_note": "unreported; count inference at the spending harness"}


def retest_fresh(*, registry: AdapterRegistry, world_version: str, target_id: str,
                 fixture: Mapping, scenario: Mapping, actor_id: str, task: Any,
                 authorize: Callable[[Action], Any | None], cap: int) -> RetestResult:
    """Run one target against a reset environment as a new experiment.

    ``authorize`` is the caller's policy: every proposal is validated as
    untrusted data before dispatch. Fresh runs may diverge from any
    recording; compare witnesses, never claim equivalence.
    """
    if not isinstance(registry, AdapterRegistry):
        raise TypeError("retest_fresh registry must be an AdapterRegistry")
    if type(world_version) is not str or not world_version:
        raise ValueError("Invalid world version")
    if type(target_id) is not str or not target_id:
        raise ValueError("Invalid target id")
    if not isinstance(fixture, Mapping) or not isinstance(scenario, Mapping):
        raise ValueError("Invalid fixture or scenario")
    if type(actor_id) is not str or not actor_id:
        raise ValueError("Invalid actor id")
    if isinstance(task, Mapping):
        task = TargetTask(task_id=task.get("task_id"), params=task.get("params"))
    if not isinstance(task, TargetTask):
        raise TypeError("retest_fresh task must be a TargetTask or mapping")
    if not callable(authorize):
        raise TypeError("retest_fresh authorize must be callable")
    if type(cap) is not int or not 1 <= cap <= 1000:
        raise ValueError("cap must be an integer from 1 to 1000")
    env_factory = registry.environment_factory(world_version)
    target = registry.target_factory(world_version, target_id)()
    env = env_factory()
    env.reset(_plain(fixture), _plain(scenario), actor_id=actor_id)
    attempt = run_attempt(env, target, task, authorize=authorize, cap=cap)
    evaluator = env_factory()
    try:
        checks = evaluator.evaluate(trace=attempt.trace, state=attempt.state)
    finally:
        evaluator.close()
    info = target.info()
    verdicts = check_verdicts(checks)
    witness = failure_witness(checks)
    denial = None if attempt.denial is None else str(attempt.denial)[:500]
    return RetestResult(
        schema=RETEST_SCHEMA, world_version=world_version,
        target_adapter_id=info.adapter_id, target_adapter_version=info.adapter_version,
        model=info.model, provider=info.provider, endpoint=info.endpoint,
        status=attempt.status, tool_calls=attempt.tool_calls, stopped=attempt.stopped,
        denied=attempt.denial is not None, denial_summary=denial, checks=verdicts, trace=_plain(attempt.trace),
        final_state=_plain(attempt.state),
        failed=tuple(sorted(name for name, hit in verdicts.items() if hit is False)),
        witness=witness, witness_sha256=witness_sha256(witness))
