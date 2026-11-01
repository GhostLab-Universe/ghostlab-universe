"""Offline qualification suite for extension environments (generic).

:func:`qualify_environment` drives any :class:`EnvironmentAdapter` through
eleven checks -- description, registration, reset correctness, run isolation,
schema agreement, unknown-tool/arg rejection, cancellation, evidence
availability, replay support and an explicit missing-capability report --
using only the frozen interfaces plus the generic runner. Each check runs in
isolation: a failure is recorded on that check, never aborts the suite, and
every environment is closed afterwards.

The suite invents no world behavior: the caller supplies a :class:`ProbeScript`
(a valid fixture, scenario and action script) plus, optionally, the expected
tool definitions (for example from :mod:`ghostlab.extensions.openapi_import`).
No network, no model calls, no snapshots assumed; worlds without snapshots
must still replay identically from recorded actions.

Standard library only.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .environment import (EXTENSION_INTERFACE_VERSION, Action, EnvironmentDescription, Observation,
                          Outcome, ToolSpec, UnsupportedOperation)
from .registry import AdapterManifest, check_environment
from .runner import replay_trace, run_attempt
from .target import FINISHED, TargetAdapter, TargetInfo, TargetTask

QUALIFIER_VERSION = "ghostlab-qualify/1"

CAPABILITY_NOTES = {
    "snapshot_restore": "no mid-run snapshots; replay re-executes recorded actions",
}


@dataclass(frozen=True)


class ProbeScript:
    """Caller-supplied valid inputs: fixture, scenario and action script."""

    fixture: str
    scenario: Mapping[str, Any]
    script: tuple[Action, ...] | list[Action]
    task_id: str = "qual-probe"
    task_params: Mapping[str, Any] | None = None

    def __post_init__(self):
        if type(self.fixture) is not str or not self.fixture or len(self.fixture) > 100:
            raise ValueError("Invalid probe fixture")
        if not isinstance(self.scenario, Mapping):
            raise ValueError("Invalid probe scenario")
        script = self.script
        if not isinstance(script, (list, tuple)) or not 1 <= len(script) <= 100:
            raise ValueError("Probe script holds 1 to 100 actions")
        for action in script:
            if not isinstance(action, Action):
                raise ValueError("Probe script holds Action entries only")
        object.__setattr__(self, "script", tuple(script))
        if type(self.task_id) is not str or not self.task_id or len(self.task_id) > 200:
            raise ValueError("Invalid probe task id")
        params = {} if self.task_params is None else self.task_params
        if not isinstance(params, Mapping):
            raise ValueError("Invalid probe task params")
        object.__setattr__(self, "task_params", dict(params))


@dataclass(frozen=True)


class QualificationCheck:
    """One named verdict with a human-readable detail line."""

    name: str
    passed: bool
    details: str

    def __post_init__(self):
        if type(self.name) is not str or not self.name:
            raise ValueError("Invalid check name")
        if type(self.passed) is not bool:
            raise ValueError("Invalid check verdict")
        if type(self.details) is not str or not self.details:
            raise ValueError("Invalid check details")

    def to_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "details": self.details}


@dataclass(frozen=True)


class QualificationReport:
    """The suite outcome: per-check verdicts plus missing capabilities."""

    world_version: str
    environment_id: str
    environment_version: str
    qualifier_version: str
    interface_version: str
    passed: bool
    checks: tuple[QualificationCheck, ...]
    missing_capabilities: tuple[str, ...]
    notes: tuple[str, ...]

    def __post_init__(self):
        for value, label in ((self.world_version, "world version"),
                             (self.environment_id, "environment id"),
                             (self.environment_version, "environment version"),
                             (self.qualifier_version, "qualifier version"),
                             (self.interface_version, "interface version")):
            if type(value) is not str or not value:
                raise ValueError(f"Invalid {label}")
        if type(self.passed) is not bool:
            raise ValueError("Invalid report verdict")
        if (not isinstance(self.checks, (list, tuple)) or not self.checks
                or any(not isinstance(check, QualificationCheck) for check in self.checks)):
            raise ValueError("Invalid report checks")
        object.__setattr__(self, "checks", tuple(self.checks))
        if (not isinstance(self.missing_capabilities, (list, tuple))
                or any(type(name) is not str for name in self.missing_capabilities)):
            raise ValueError("Invalid missing capabilities")
        object.__setattr__(self, "missing_capabilities", tuple(self.missing_capabilities))
        if (not isinstance(self.notes, (list, tuple))
                or any(type(note) is not str for note in self.notes)):
            raise ValueError("Invalid report notes")
        object.__setattr__(self, "notes", tuple(self.notes))

    def to_dict(self) -> dict:
        return {"world_version": self.world_version, "environment_id": self.environment_id,
                "environment_version": self.environment_version,
                "qualifier_version": self.qualifier_version,
                "interface_version": self.interface_version, "passed": self.passed,
                "checks": [check.to_dict() for check in self.checks],
                "missing_capabilities": list(self.missing_capabilities),
                "notes": list(self.notes)}

    def summary(self) -> str:
        done = sum(1 for check in self.checks if check.passed)
        missing = ", ".join(self.missing_capabilities) if self.missing_capabilities else "none"
        verdict = "PASS" if self.passed else "FAIL"
        return (f"{self.world_version} qualification {verdict} "
                f"({done}/{len(self.checks)} checks; missing: {missing})")


class ScriptTarget:
    """Fixed-script probe target: proposes each action once, then finishes."""

    def __init__(self, actions, adapter_id: str = "qual-probe"):
        if type(adapter_id) is not str or not adapter_id or len(adapter_id) > 200:
            raise ValueError("Invalid probe adapter id")
        actions = list(actions)
        for action in actions:
            if not isinstance(action, Action):
                raise ValueError("Probe targets propose Action entries only")
        self._script = actions
        self._adapter_id = adapter_id
        self._results: list = []
        self._started = False
        self.closed_with: str | None = None

    def info(self) -> TargetInfo:
        return TargetInfo(
            adapter_id=self._adapter_id, adapter_version="qualify/1", model=None,
            provider=None, endpoint=None,
            actions=tuple({"tool": action.tool, "arguments": dict(action.arguments),
                           "status": outcome.status}
                          for action, outcome in self._results))

    def start(self, task: TargetTask, observation) -> None:
        if self._started:
            raise RuntimeError("Probe target is already started")
        if not isinstance(task, TargetTask):
            raise TypeError("Probe target needs a TargetTask")
        if not isinstance(observation, Observation):
            raise TypeError("Probe target needs an Observation")
        self._started = True

    def propose(self, observation) -> Action | None:
        if not self._started or self.closed_with is not None:
            raise RuntimeError("Probe target is not running")
        if not isinstance(observation, Observation):
            raise TypeError("Probe target needs an Observation")
        if not self._script:
            return None
        return self._script.pop(0)

    def notify_result(self, action: Action, outcome: Outcome) -> None:
        if not isinstance(action, Action) or not isinstance(outcome, Outcome):
            raise TypeError("Probe results need an Action and an Outcome")
        self._results.append((action, outcome))

    def close(self, reason: str) -> None:
        if type(reason) is not str or not reason:
            raise ValueError("Invalid probe close reason")
        if self.closed_with is None:
            self.closed_with = reason


assert isinstance(ScriptTarget([]), TargetAdapter)


def qualify_environment(*, world_version: str, env_factory, manifest: AdapterManifest,
                        probe: ProbeScript, expected_tools=None,
                        actor_id: str = "qual-probe") -> QualificationReport:
    """Qualify one environment factory against the frozen interfaces.

    ``probe`` supplies a valid fixture, scenario and action script;
    ``expected_tools`` optionally supplies the tool definitions the adapter
    must agree with (names, required parameters, property types and bounds).
    Every check is recorded; the suite never aborts early and never touches
    the network, a model, or an optional capability it has not verified.
    """
    if type(world_version) is not str or not world_version:
        raise ValueError("world_version must be a non-empty string")
    if not callable(env_factory):
        raise ValueError("env_factory must be callable")
    if not isinstance(manifest, AdapterManifest):
        raise ValueError("manifest must be an AdapterManifest")
    if not isinstance(probe, ProbeScript):
        raise ValueError("probe must be a ProbeScript")
    if expected_tools is None:
        expected = None
    else:
        if not isinstance(expected_tools, (list, tuple)) or not expected_tools:
            raise ValueError("expected_tools must be a non-empty list or None")
        for tool in expected_tools:
            if not isinstance(tool, ToolSpec):
                raise ValueError("expected_tools holds ToolSpec entries only")
        names = [tool.name for tool in expected_tools]
        if len(set(names)) != len(names):
            raise ValueError("expected_tools holds duplicate names")
        expected = list(expected_tools)
    if type(actor_id) is not str or not actor_id or len(actor_id) > 100:
        raise ValueError("Invalid actor_id")

    checks: list[QualificationCheck] = []

    def make():
        env = env_factory()
        check_environment(env)
        return env

    def close(env) -> None:
        try:
            env.close()
        except Exception:
            pass

    def reset(env):
        return env.reset(env.named_fixture(probe.fixture), dict(probe.scenario),
                         actor_id=actor_id)

    def task() -> TargetTask:
        return TargetTask(task_id=probe.task_id, params=dict(probe.task_params))

    def run(name: str, fn) -> None:
        try:
            details = fn()
            if type(details) is not str or not details:
                raise AssertionError("empty check details")
            checks.append(QualificationCheck(name=name, passed=True, details=details))
        except AssertionError as failed:
            checks.append(QualificationCheck(
                name=name, passed=False, details=str(failed) or name))
        except Exception as error:  # noqa: BLE001 -- recorded, never raised
            checks.append(QualificationCheck(
                name=name, passed=False, details=f"{type(error).__name__}: {error}"))

    def expect(condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def check_describe():
        env = make()
        try:
            description = env.describe()
            expect(isinstance(description, EnvironmentDescription),
                   "describe() must return EnvironmentDescription")
            expect(description.world_version == world_version,
                   f"world {description.world_version!r} != {world_version!r}")
            expect(bool(description.source), "description needs a source")
            expect(description.capabilities.get("snapshot_restore") in (True, False),
                   "capabilities must declare snapshot_restore")
            return (f"{description.environment_id}@{description.environment_version}: "
                    f"{len(description.tools)} tools, {len(description.faults)} faults, "
                    f"{len(description.invariants)} invariants, "
                    f"{len(description.fixtures)} fixtures")
        finally:
            close(env)

    def check_registration():
        expect(manifest.origin == "checked_in",
               f"origin {manifest.origin!r} is not checked_in")
        env = make()
        try:
            return f"{manifest.package}@{manifest.version} ({type(env).__name__})"
        finally:
            close(env)

    def check_reset():
        env = make()
        try:
            try:
                env.named_fixture("qual-no-such-fixture")
            except KeyError:
                pass
            else:
                raise AssertionError("unknown fixture names must raise KeyError")
            for bad_fixture in ("nope", {}, {"fixture": "qual-no-such-fixture"}):
                try:
                    env.reset(bad_fixture, dict(probe.scenario), actor_id=actor_id)
                except ValueError:
                    pass
                else:
                    raise AssertionError(
                        f"bad fixture {bad_fixture!r} must raise ValueError")
            try:
                env.reset(env.named_fixture(probe.fixture), dict(probe.scenario), actor_id="")
            except ValueError:
                pass
            else:
                raise AssertionError("empty actor_id must raise ValueError")
            try:
                env.reset(env.named_fixture(probe.fixture), "nope", actor_id=actor_id)
            except ValueError:
                pass
            else:
                raise AssertionError("non-mapping scenario must raise ValueError")
            observed = reset(env)
            expect(observed.calls == 0 and observed.trace == [], "reset must start clean")
            expect(env.faults_fired(dict(probe.scenario), []) == frozenset(),
                   "an empty trace fires no faults")
            reset(env)
            expect(env.observe().calls == 0, "a second reset must start clean")
            return f"fixture {probe.fixture!r} resets clean; invalid input refused"
        finally:
            close(env)

    def check_isolation():
        first, second = make(), make()
        try:
            reset(first)
            reset(second)
            for action in probe.script:
                outcome = first.execute(action)
                expect(isinstance(outcome, Outcome), "execute must return Outcome")
            expect(first.observe().calls == len(probe.script),
                   "the first run must apply every action")
            expect(second.observe().calls == 0 and second.observe().trace == [],
                   "the second run must stay untouched")
            for action in probe.script:
                second.execute(action)
            expect(second.observe().trace == first.observe().trace,
                   "identical scripts must trace identically")
            expect(second.observe().state == first.observe().state,
                   "identical scripts must end identically")
            snooped = first.observe()
            snooped.trace.append({"forged": True})
            if isinstance(snooped.state, dict):
                snooped.state["forged"] = True
            expect(first.observe().trace == second.observe().trace,
                   "observed traces must be private copies")
            expect(first.observe().state == second.observe().state,
                   "observed states must be private copies")
            reset(first)
            expect(first.observe().calls == 0, "reset must clean only its own run")
            expect(second.observe().calls == len(probe.script),
                   "reset must not touch sibling runs")
            return f"{len(probe.script)} actions isolated across two instances"
        finally:
            close(first)
            close(second)

    def check_agreement():
        if expected is None:
            return "skipped (no expected tools supplied)"
        env = make()
        try:
            described = {tool.name: tool for tool in env.describe().tools}
        finally:
            close(env)
        expect(set(described) == {tool.name for tool in expected},
               f"tool names {sorted(described)} != expected "
               f"{sorted(tool.name for tool in expected)}")
        for want in expected:
            got = described[want.name]
            for label in ("required", "properties"):
                for tool, side in ((got, "adapter"), (want, "expected")):
                    shape = tool.parameters.get(label) if isinstance(
                        tool.parameters, Mapping) else None
                    if label == "required" and not isinstance(shape, list):
                        raise AssertionError(
                            f"{side} tool {tool.name!r} has no comparable required list")
                    if label == "properties" and not isinstance(shape, Mapping):
                        raise AssertionError(
                            f"{side} tool {tool.name!r} has no comparable properties")
            expect(sorted(got.parameters["required"]) == sorted(want.parameters["required"]),
                   f"{want.name}: required {got.parameters['required']}"
                   f" != expected {want.parameters['required']}")
            have_props, need_props = got.parameters["properties"], want.parameters["properties"]
            expect(set(have_props) == set(need_props),
                   f"{want.name}: properties {sorted(have_props)}"
                   f" != expected {sorted(need_props)}")
            for prop in sorted(need_props):
                if not isinstance(have_props[prop], Mapping):
                    raise AssertionError(f"{want.name}.{prop} is not comparable")
                for key in ("type", "minLength", "maxLength", "minimum", "maximum"):
                    expect(have_props[prop].get(key) == need_props[prop].get(key),
                           f"{want.name}.{prop}.{key}: {have_props[prop].get(key)!r}"
                           f" != expected {need_props[prop].get(key)!r}")
        return f"{len(expected)} tool definitions agree with describe()"
    def check_unknown_tool():
        env = make()
        try:
            reset(env)
            before = env.observe()
            outcome = env.execute(Action(tool="qual_no_such_tool", arguments={}))
            expect(isinstance(outcome, Outcome) and outcome.status == "error",
                   "unknown tools must be recorded errors")
            after = env.observe()
            expect(after.state == before.state, "rejected tools must not mutate state")
            expect(after.calls == before.calls + 1
                   and len(after.trace) == len(before.trace) + 1,
                   "rejected tools must append exactly one event")
            return f"recorded {outcome.error}"
        finally:
            close(env)

    def check_unknown_arg():
        env = make()
        try:
            reset(env)
            tool_name = env.describe().tools[0].name
            before = env.observe()
            try:
                outcome = env.execute(
                    Action(tool=tool_name, arguments={"__qual_unknown_arg__": 1}))
            except (TypeError, ValueError) as refused:
                after = env.observe()
                expect(after.state == before.state, "refused args must not mutate state")
                expect(after.calls == before.calls and after.trace == before.trace,
                       "refused args must append no event")
                return f"raised {type(refused).__name__} without touching state"
            expect(isinstance(outcome, Outcome), "execute must return Outcome or raise")
            after = env.observe()
            expect(after.state == before.state, "unknown args must not mutate state")
            if outcome.status == "ok":
                expect(after.calls == before.calls + 1,
                       "applied actions append exactly one event")
                return "ignored without effect"
            expect(after.calls == before.calls + 1
                   and len(after.trace) == len(before.trace) + 1,
                   "rejected args must append exactly one event")
            return f"recorded {outcome.error}"
        finally:
            close(env)

    def check_cancellation():
        env = make()
        reset(env)
        target = ScriptTarget(list(probe.script))
        authorized: list = []

        def authorize(action):
            authorized.append(action)
            return None

        def stop_after_one():
            return "cancelled" if len(authorized) >= 1 else None

        result = run_attempt(env, target, task(), authorize=authorize,
                             cap=len(probe.script) + 5, should_stop=stop_after_one,
                             cooperative_stop=True)
        expect(result.stopped == "cancelled", "the stop reason must be reported")
        expect(result.tool_calls == 1, "the stop must land after the first action")
        expect(target.closed_with == "cancelled",
               "the target must close with the stop reason")
        patient, target2 = make(), ScriptTarget(list(probe.script))
        reset(patient)
        full = run_attempt(patient, target2, task(), authorize=lambda action: None,
                           cap=len(probe.script) + 5, should_stop=lambda: "cancelled",
                           cooperative_stop=False)
        expect(full.stopped is None and full.tool_calls == len(probe.script),
               "non-cooperative runs ignore the stop flag")
        expect(target2.closed_with == FINISHED, "completed targets close finished")
        return "cooperative stop lands; non-cooperative runs complete"

    def check_evidence():
        env = make()
        reset(env)
        target = ScriptTarget(list(probe.script))
        result = run_attempt(env, target, task(), authorize=lambda action: None,
                             cap=len(probe.script) + 5, cooperative_stop=False)
        expect(len(result.trace) == len(probe.script),
               "every applied action appends exactly one event")
        for event in result.trace:
            expect(isinstance(event.get("request"), Mapping)
                   and isinstance(event.get("outcome"), Mapping),
                   "events carry request and outcome")
        expect(result.tool_calls == len(probe.script), "the call count must match")
        live = make()
        try:
            found = live.evaluate(trace=result.trace, state=result.state)
            names = {check.name for check in found}
            expect(names == set(live.describe().invariants),
                   f"checks {sorted(names)} must cover the declared invariants")
            for check in found:
                if not check.passed:
                    expect(check.evidence_event_ids,
                           f"failed check {check.name} must cite events")
            other = make()
            try:
                expect(other.evaluate(trace=result.trace, state=result.state) == found,
                       "evaluate must be pure")
            finally:
                close(other)
            passed = sum(1 for check in found if check.passed)
            return f"{passed}/{len(found)} invariants hold; evidence cites events"
        finally:
            close(live)

    def check_replay():
        env = make()
        reset(env)
        target = ScriptTarget(list(probe.script))
        recorded = run_attempt(env, target, task(), authorize=lambda action: None,
                               cap=len(probe.script) + 5, cooperative_stop=False)
        fresh = make()
        try:
            reset(fresh)
            replayed = replay_trace(fresh, recorded.trace)
            expect(list(replayed.trace) == recorded.trace,
                   "replay must reproduce the trace")
            expect(dict(replayed.state) == dict(recorded.state),
                   "replay must reproduce the state")
            if fresh.describe().capabilities.get("snapshot_restore") is True:
                check = make()
                try:
                    reset(check)
                    check.execute(probe.script[0])
                    mid = check.observe()
                    snap = check.snapshot()
                    for action in probe.script[1:]:
                        check.execute(action)
                    restored = check.restore(snap)
                    expect(list(restored.trace) == list(mid.trace)
                           and dict(restored.state) == dict(mid.state)
                           and restored.calls == mid.calls,
                           "restore must return to the snapshot")
                finally:
                    close(check)
                return "recorded replay matches; snapshots round-trip"
            try:
                fresh.snapshot()
            except UnsupportedOperation:
                pass
            else:
                raise AssertionError(
                    "snapshot() must raise UnsupportedOperation when unsupported")
            try:
                fresh.restore({})
            except UnsupportedOperation:
                pass
            else:
                raise AssertionError(
                    "restore() must raise UnsupportedOperation when unsupported")
            return "recorded replay matches without snapshots"
        finally:
            close(fresh)

    def check_missing():
        env = make()
        try:
            capabilities = env.describe().capabilities
        finally:
            close(env)
        absent = sorted(name for name, supported in capabilities.items()
                        if supported is not True)
        if not absent:
            return "no missing capabilities"
        return "; ".join(f"{name}: {CAPABILITY_NOTES.get(name, 'not supported')}"
                         for name in absent)

    run("describe", check_describe)
    run("registration", check_registration)
    run("reset_correctness", check_reset)
    run("run_isolation", check_isolation)
    run("schema_agreement", check_agreement)
    run("unknown_tool_rejection", check_unknown_tool)
    run("unknown_arg_rejection", check_unknown_arg)
    run("cancellation", check_cancellation)
    run("evidence_availability", check_evidence)
    run("replay_support", check_replay)
    run("missing_capabilities", check_missing)

    capabilities: Mapping = {}
    identity = ("unknown", "unknown")
    try:
        env = make()
        try:
            described = env.describe()
            capabilities = dict(described.capabilities)
            identity = (described.environment_id, described.environment_version)
        finally:
            close(env)
    except Exception:
        pass
    missing = tuple(sorted(name for name, supported in capabilities.items()
                           if supported is not True))
    notes = (f"interface {EXTENSION_INTERFACE_VERSION}", f"qualifier {QUALIFIER_VERSION}",
             f"script of {len(probe.script)} actions on fixture {probe.fixture!r}",
             "offline: no network, no model calls, no snapshots assumed")
    return QualificationReport(
        world_version=world_version, environment_id=identity[0],
        environment_version=identity[1], qualifier_version=QUALIFIER_VERSION,
        interface_version=EXTENSION_INTERFACE_VERSION,
        passed=all(check.passed for check in checks), checks=tuple(checks),
        missing_capabilities=missing, notes=notes)
