"""P1 interface conformance: registry, runner and helpers against stub adapters.

Standard library only (unittest style): these tests prove the frozen
extension interfaces work with no world knowledge. The stub world is a
counter: ``bump`` increments it, the ``skip`` fault drops the first bump.
"""

import unittest
from collections.abc import Mapping
from copy import deepcopy

from ghostlab.extensions.environment import (Action, ActionBudgetExceeded,
                                             EnvironmentAdapter, EnvironmentDescription,
                                             FaultSpec, InvariantCheck, Observation,
                                             Outcome, ToolSpec, UnsupportedOperation)
from ghostlab.extensions.registry import (AdapterManifest, AdapterRegistry,
                                          RegistrationError, UnknownTargetError,
                                          UnknownWorldError, check_environment,
                                          check_target)
from ghostlab.extensions.runner import (AttemptResult, first_divergence, replay_trace,
                                        run_attempt)
from ghostlab.extensions.target import (TargetAdapter, TargetInfo, TargetTask)


class StubEnvironment:
    """Counter world without snapshots: the runner must never assume them."""

    def __init__(self):
        self.executions = 0
        self.snapshot_calls = 0
        self._trace = []
        self._state = {"count": 0}
        self._calls = 0
        self._fault = False
        self._fired = False
        self._closed = False

    def describe(self):
        return EnvironmentDescription(
            world_version="stub-world/1", environment_id="stub",
            environment_version="stub/1",
            tools=[ToolSpec(name="bump", effect="mutate", parameters={},
                            description="Increment the counter")],
            faults=[FaultSpec(fault_id="skip", trigger_points=("first_bump",),
                              description="The first bump is dropped")],
            invariants=["counted"], fixtures=["zero"],
            capabilities={"snapshot_restore": False}, source="p1-test-stub")

    def named_fixture(self, name):
        if name != "zero":
            raise KeyError(name)
        return {}

    def reset(self, fixture, scenario, *, actor_id):
        if not isinstance(fixture, Mapping) or not isinstance(scenario, Mapping):
            raise ValueError("Invalid fixture or scenario")
        if type(actor_id) is not str or not actor_id:
            raise ValueError("Invalid actor")
        self._trace, self._state, self._calls = [], {"count": 0}, 0
        self._fault, self._fired, self._closed = bool(scenario.get("skip_first")), False, False
        return self.observe()

    def execute(self, action):
        if self._closed:
            raise RuntimeError("closed")
        if not isinstance(action, Action):
            raise TypeError("Stub actions must be Action")
        self.executions += 1
        self._calls += 1
        if action.tool != "bump" or dict(action.arguments):
            outcome = {"error": "UnknownTool", "message": "Unknown tool"}
            recorded = Outcome(status="error", event_id=self._calls, error="UnknownTool",
                               message="Unknown tool")
        elif self._fault and not self._fired:
            self._fired = True
            outcome = {"error": "Skipped", "message": "First bump dropped"}
            recorded = Outcome(status="error", event_id=self._calls, error="Skipped",
                               message="First bump dropped")
        else:
            self._state = {"count": self._state["count"] + 1}
            outcome = {"result": {"count": self._state["count"]}}
            recorded = Outcome(status="ok", event_id=self._calls,
                               result={"count": self._state["count"]})
        self._trace.append({"event_id": self._calls,
                            "request": {"tool": action.tool,
                                        "arguments": dict(action.arguments)},
                            "outcome": outcome, "state": dict(self._state)})
        return recorded

    def observe(self):
        if self._closed:
            raise RuntimeError("closed")
        return Observation(trace=deepcopy(self._trace), state=dict(self._state),
                           calls=self._calls)

    def evaluate(self, *, trace, state):
        if type(trace) is not list or not isinstance(state, Mapping):
            raise ValueError("Invalid evidence")
        return [InvariantCheck(name="counted", passed=True, evidence_event_ids=(),
                               observed=state.get("count", 0), limit=1000,
                               unit="bumps")]

    def faults_fired(self, scenario, trace):
        if not isinstance(scenario, Mapping) or type(trace) is not list:
            raise ValueError("Invalid fault evidence")
        return frozenset({"skip"}) if self._fired else frozenset()

    def close(self):
        self._closed = True

    def snapshot(self):
        self.snapshot_calls += 1
        raise UnsupportedOperation("stub has no snapshots")

    def restore(self, snapshot):
        self.snapshot_calls += 1
        raise UnsupportedOperation("stub has no snapshots")


class ScriptTarget:
    """Proposes a fixed script of actions, then finishes."""

    def __init__(self, actions, adapter_id="stub-script"):
        self._script = list(actions)
        self._adapter_id = adapter_id
        self._results = []
        self._started = False
        self.closed_with = None

    def info(self):
        return TargetInfo(
            adapter_id=self._adapter_id, adapter_version="stub/1", model=None,
            provider=None, endpoint=None,
            actions=tuple({"tool": action.tool, "arguments": dict(action.arguments),
                           "status": outcome.status}
                          for action, outcome in self._results))

    def start(self, task, observation):
        if self._started:
            raise RuntimeError("already started")
        if not isinstance(task, TargetTask):
            raise TypeError("ScriptTarget needs a TargetTask")
        self._started = True

    def propose(self, observation):
        if not self._started or self.closed_with is not None:
            raise RuntimeError("target is not running")
        if not self._script:
            return None
        return self._script.pop(0)

    def notify_result(self, action, outcome):
        self._results.append((action, outcome))

    def close(self, reason):
        if self.closed_with is None:
            self.closed_with = reason


MANIFEST = AdapterManifest(package="p1-test-stub", version="stub/1",
                           origin="checked_in", description="test stub")


def bump():
    return Action(tool="bump", arguments={})


def task():
    return TargetTask(task_id="stub-task", params={})


def registry():
    register = AdapterRegistry()
    register.register_environment("stub-world/1", StubEnvironment, MANIFEST)
    register.register_target("stub-world/1", "stub-script", lambda: ScriptTarget([bump()]),
                             MANIFEST)
    return register


class ProtocolTests(unittest.TestCase):
    def test_stubs_satisfy_the_protocols(self):
        check_environment(StubEnvironment())
        check_target(ScriptTarget([]))
        self.assertIsInstance(StubEnvironment(), EnvironmentAdapter)
        self.assertIsInstance(ScriptTarget([]), TargetAdapter)

    def test_incomplete_objects_are_refused(self):
        with self.assertRaises(RegistrationError):
            check_environment(object())
        with self.assertRaises(RegistrationError):
            check_target(object())


class RegistryTests(unittest.TestCase):
    def test_register_and_resolve(self):
        register = registry()
        self.assertIs(register.environment_factory("stub-world/1"), StubEnvironment)
        target = register.target_factory("stub-world/1", "stub-script")()
        self.assertIsInstance(target, TargetAdapter)
        self.assertEqual(register.worlds(), ("stub-world/1",))
        self.assertEqual(register.targets("stub-world/1"), ("stub-script",))
        self.assertIs(register.environment_manifest("stub-world/1"), MANIFEST)

    def test_unknown_ids_fail_closed(self):
        register = registry()
        with self.assertRaises(UnknownWorldError):
            register.environment_factory("nope/1")
        with self.assertRaises(UnknownWorldError):
            register.target_factory("nope/1", "stub-script")
        with self.assertRaises(UnknownTargetError):
            register.target_factory("stub-world/1", "nope")

    def test_only_checked_in_origins_register(self):
        with self.assertRaises(ValueError):
            AdapterManifest(package="evil", version="1", origin="uploaded")
        register = AdapterRegistry()
        with self.assertRaises(RegistrationError):
            register.register_environment("stub-world/1", StubEnvironment, "not-a-manifest")
        with self.assertRaises(RegistrationError):
            register.register_environment("stub-world/1", None, MANIFEST)

    def test_duplicates_need_explicit_replace(self):
        register = registry()
        with self.assertRaises(RegistrationError):
            register.register_environment("stub-world/1", StubEnvironment, MANIFEST)
        with self.assertRaises(RegistrationError):
            register.register_target("stub-world/1", "stub-script",
                                     StubEnvironment, MANIFEST)
        register.register_environment("stub-world/1", StubEnvironment, MANIFEST,
                                      replace=True)
        self.assertEqual(register.worlds(), ("stub-world/1",))

    def test_no_code_loading_api_exists(self):
        exposed = {name for name in dir(AdapterRegistry) if not name.startswith("_")}
        loaders = {"load", "import_module", "importlib", "exec", "eval", "from_path",
                   "load_path", "install", "fetch"}
        self.assertEqual(exposed & loaders, set())


class RunnerTests(unittest.TestCase):
    def run_script(self, count, **options):
        env = StubEnvironment()
        env.reset({}, {}, actor_id="tester")
        target = ScriptTarget([bump() for _ in range(count)])
        options.setdefault("authorize", lambda action: None)
        options.setdefault("cap", 10)
        return env, target, run_attempt(env, target, task(), **options)

    def test_happy_path(self):
        env, target, result = self.run_script(3)
        self.assertIsInstance(result, AttemptResult)
        self.assertEqual((result.status, result.tool_calls), ("completed", 3))
        self.assertIsNone(result.denial)
        self.assertIsNone(result.stopped)
        self.assertEqual(result.state, {"count": 3})
        self.assertEqual([event["event_id"] for event in result.trace], [1, 2, 3])
        self.assertEqual(target.closed_with, "finished")
        self.assertEqual(target.info().adapter_id, "stub-script")
        with self.assertRaises(RuntimeError):
            env.observe()

    def test_empty_script_finishes_without_calls(self):
        _, target, result = self.run_script(0)
        self.assertEqual((result.status, result.tool_calls), ("completed", 0))
        self.assertEqual(target.closed_with, "finished")

    def test_cap_bounds_applied_actions(self):
        env, target, result = self.run_script(5, cap=2)
        self.assertEqual((result.status, result.tool_calls), ("budget_exhausted", 2))
        self.assertEqual(len(result.trace), 2)
        self.assertEqual(target.closed_with, "budget_exhausted")

    def test_denial_stops_before_dispatch(self):
        env = StubEnvironment()
        env.reset({}, {}, actor_id="tester")
        target = ScriptTarget([bump(), bump()])
        denial = object()
        calls = []
        result = run_attempt(env, target, task(), cap=10,
                             authorize=lambda action: calls.append(action) or (
                                 None if len(calls) == 1 else denial))
        self.assertIs(result.denial, denial)
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(env.executions, 1)
        self.assertEqual(target.closed_with, "denied")

    def test_denial_wins_over_an_exhausted_cap(self):
        env = StubEnvironment()
        env.reset({}, {}, actor_id="tester")
        target = ScriptTarget([bump(), bump()])
        denial = object()
        seen = []
        result = run_attempt(env, target, task(), cap=1,
                             authorize=lambda action: seen.append(action) or (
                                 None if len(seen) == 1 else denial))
        self.assertIs(result.denial, denial)
        self.assertEqual(result.tool_calls, 1)

    def test_stop_discards_the_partial_run(self):
        env = StubEnvironment()
        env.reset({}, {}, actor_id="tester")
        target = ScriptTarget([bump(), bump()])
        checks = []
        result = run_attempt(env, target, task(), cap=10,
                             authorize=lambda action: None,
                             should_stop=lambda: checks.append(1) or (
                                 None if len(checks) == 1 else "cancelled"))
        self.assertEqual(result.stopped, "cancelled")
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(target.closed_with, "cancelled")

    def test_stop_is_ignored_when_not_cooperative(self):
        _, _, result = self.run_script(2, should_stop=lambda: "cancelled",
                                       cooperative_stop=False)
        self.assertEqual((result.status, result.tool_calls), ("completed", 2))
        self.assertIsNone(result.stopped)

    def test_environment_budget_maps_to_budget_exhausted(self):
        class Capped(StubEnvironment):
            def execute(self, action):
                if self._calls >= 1:
                    raise ActionBudgetExceeded("capped")
                return super().execute(action)

        env = Capped()
        env.reset({}, {}, actor_id="tester")
        target = ScriptTarget([bump(), bump()])
        result = run_attempt(env, target, task(), authorize=lambda action: None, cap=10)
        self.assertEqual((result.status, result.tool_calls), ("budget_exhausted", 1))

    def test_target_failure_propagates_after_closing(self):
        env = StubEnvironment()
        env.reset({}, {}, actor_id="tester")

        class Exploding(ScriptTarget):
            def propose(self, observation):
                raise RuntimeError("boom")

        target = Exploding([])
        with self.assertRaises(RuntimeError):
            run_attempt(env, target, task(), authorize=lambda action: None, cap=10)
        self.assertEqual(target.closed_with, "error")
        with self.assertRaises(RuntimeError):
            env.observe()

    def test_snapshots_are_never_assumed(self):
        env, _, result = self.run_script(2)
        self.assertEqual(result.status, "completed")
        self.assertEqual(env.snapshot_calls, 0)

    def test_interfaces_are_validated_upfront(self):
        env = StubEnvironment()
        env.reset({}, {}, actor_id="tester")
        target = ScriptTarget([])
        with self.assertRaises(RegistrationError):
            run_attempt(object(), target, task(), authorize=lambda action: None, cap=1)
        with self.assertRaises(RegistrationError):
            run_attempt(env, object(), task(), authorize=lambda action: None, cap=1)
        with self.assertRaises(ValueError):
            run_attempt(env, target, task(), authorize=lambda action: None, cap=0)


class FirstDivergenceTests(unittest.TestCase):
    def test_equal_traces_have_no_divergence(self):
        trace = [{"event_id": 1}, {"event_id": 2}]
        self.assertIsNone(first_divergence(trace, [dict(event) for event in trace]))
        self.assertIsNone(first_divergence([], []))

    def test_first_difference_wins(self):
        baseline = [{"event_id": 1, "x": 1}, {"event_id": 2, "x": 2}]
        faulted = [{"event_id": 1, "x": 1}, {"event_id": 2, "x": 3}]
        self.assertEqual(first_divergence(baseline, faulted),
                         {"event_id": 2, "baseline": baseline[1], "faulted": faulted[1]})

    def test_length_mismatch_diverges_where_the_shorter_ends(self):
        self.assertEqual(first_divergence([{"event_id": 1}], [])["event_id"], 1)
        self.assertEqual(first_divergence([], [{"event_id": 1}])["event_id"], 1)

    def test_invalid_traces_raise(self):
        with self.assertRaises(ValueError):
            first_divergence({}, [])


class ReplayTests(unittest.TestCase):
    def test_replay_reproduces_the_recording(self):
        env = StubEnvironment()
        env.reset({}, {"skip_first": True}, actor_id="tester")
        target = ScriptTarget([bump(), bump()])
        recorded = run_attempt(env, target, task(), authorize=lambda action: None, cap=10)
        fresh = StubEnvironment()
        fresh.reset({}, {"skip_first": True}, actor_id="tester")
        replayed = replay_trace(fresh, recorded.trace)
        self.assertEqual(replayed.trace, recorded.trace)
        self.assertEqual(replayed.state, recorded.state)
        self.assertEqual(fresh.snapshot_calls, 0)

    def test_tampered_requests_do_not_reproduce(self):
        env = StubEnvironment()
        env.reset({}, {}, actor_id="tester")
        target = ScriptTarget([bump()])
        recorded = run_attempt(env, target, task(), authorize=lambda action: None, cap=10)
        tampered = deepcopy(recorded.trace)
        tampered[0]["request"]["tool"] = "forged"
        fresh = StubEnvironment()
        fresh.reset({}, {}, actor_id="tester")
        replayed = replay_trace(fresh, tampered)
        self.assertNotEqual(replayed.trace, recorded.trace)

    def test_malformed_events_raise(self):
        env = StubEnvironment()
        env.reset({}, {}, actor_id="tester")
        with self.assertRaises(ValueError):
            replay_trace(env, [{"no_request": True}])


if __name__ == "__main__":
    unittest.main()