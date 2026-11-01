"""P2 reservation world: reviewed local adapter behind the frozen interfaces.

Standard library only (unittest style). The reservation service was developed
externally and vendored byte-identical; every test here drives it through the
frozen ``ghostlab-extensions/1`` interfaces plus the generic runner. Direct
service access appears only where a test asserts adapter/service agreement.

Re-vendor procedure (review-time check): copy reservation_service.py at a
reviewed ghostlab-ext-example commit, then update VENDOR_SHA256 and
SERVICE_PROVENANCE together. The differential test below fails while the
external file differs from the pin.
"""

import hashlib
import os
import unittest
from copy import deepcopy
from pathlib import Path

import ghostlab.adapters.reservation_env as reservation_env
import ghostlab.adapters.reservation_service as vendored
from ghostlab.adapters.reservation_env import (
    ENVIRONMENT_MANIFEST, RESERVATION_TOOL_SCHEMAS, RESERVATION_WORLD_VERSION,
    SERVICE_PROVENANCE, SKIP_FAULT, STALE_FAULT, TIMEOUT_FAULT, VENDOR_SHA256,
    ReservationEnvironment, register_reservation_adapters, revision_growth_events)
from ghostlab.adapters.reservation_service import ReservationService, TimeoutAfterCommit
from ghostlab.extensions.environment import (Action, ActionBudgetExceeded, EnvironmentAdapter,
                                             UnsupportedOperation)
from ghostlab.extensions.qualify import ScriptTarget
from ghostlab.extensions.registry import (AdapterRegistry, RegistrationError, UnknownTargetError,
                                          UnknownWorldError)
from ghostlab.extensions.runner import first_divergence, replay_trace, run_attempt
from ghostlab.extensions.target import TargetTask

FIXTURE = "half_full"
CLEAN_SCENARIO = {"timeout_after_commit": False, "stale_snapshot": False,
                  "skip_capacity_check": False, "max_tool_calls": 20}

# Where the outside-the-tree service may live. The differential test skips
# when none exists (e.g. CI); it never vendors or executes from here.
EXTERNAL_SERVICE_CANDIDATES = tuple(p for p in (
    Path(os.environ["GHOSTLAB_EXT_EXAMPLE"]) / "reservation_service.py"
    if os.environ.get("GHOSTLAB_EXT_EXAMPLE") else None,
    Path(__file__).resolve().parents[4] / "ghostlab-ext-example"
    / "reservation_service.py",
) if p is not None)


def fresh(fixture=FIXTURE, scenario=None, actor="qual-probe"):
    env = ReservationEnvironment()
    env.reset(env.named_fixture(fixture),
              dict(CLEAN_SCENARIO if scenario is None else scenario), actor_id=actor)
    return env


def script_actions():
    """Six valid calls on half_full (used 5/10): read, book, inspect, free, inspect, miss."""
    return [
        Action(tool="check_availability", arguments={}),
        Action(tool="reserve", arguments={"holder": "carol", "seats": 2}),
        Action(tool="get_status", arguments={"reservation_id": "R-0003"}),
        Action(tool="cancel", arguments={"reservation_id": "R-0003"}),
        Action(tool="get_status", arguments={"reservation_id": "R-0003"}),
        Action(tool="cancel", arguments={"reservation_id": "R-9999"}),
    ]


def drive(script, fixture=FIXTURE, scenario=None):
    env = fresh(fixture, scenario)
    target = ScriptTarget(list(script))
    result = run_attempt(env, target, TargetTask(task_id="p2", params={}),
                         authorize=lambda action: None, cap=len(script) + 5,
                         cooperative_stop=False)
    return result, target


def service_checks(actions, fixture, scenario):
    """Independent detector: the raw service over the same calls."""
    service = ReservationService(fixture)
    for fault_id in ("timeout_after_commit", "stale_snapshot", "skip_capacity_check"):
        if scenario.get(fault_id):
            service.set_fault(fault_id, True)
    for action in actions:
        try:
            service.call_tool(action.tool, dict(action.arguments))
        except TimeoutAfterCommit:
            pass  # Committed, exactly as on the adapter path.
    return service.check_invariants()


class DescribeTests(unittest.TestCase):
    def test_description(self):
        description = ReservationEnvironment().describe()
        self.assertEqual(description.world_version, RESERVATION_WORLD_VERSION)
        self.assertEqual(description.environment_id, "reservation-environment")
        self.assertEqual(description.environment_version, "reservation-environment/1")
        self.assertEqual([tool.name for tool in description.tools],
                         ["check_availability", "reserve", "cancel", "get_status"])
        self.assertEqual([tool.effect for tool in description.tools],
                         ["read", "mutate", "mutate", "read"])
        self.assertEqual([fault.fault_id for fault in description.faults],
                         [TIMEOUT_FAULT, STALE_FAULT, SKIP_FAULT])
        for fault in description.faults:
            self.assertTrue(fault.trigger_points, fault.fault_id)
        self.assertEqual(tuple(description.invariants),
                         ("capacity_respected", "records_wellformed", "used_consistent",
                          "used_nonnegative"))
        self.assertEqual(tuple(description.fixtures),
                         ("empty_small", "full", "half_full", "near_full"))
        self.assertEqual(description.capabilities, {"snapshot_restore": False})
        self.assertIn("checked-in", description.source)

    def test_tool_schemas(self):
        schemas = RESERVATION_TOOL_SCHEMAS
        self.assertEqual(sorted(schemas),
                         ["cancel", "check_availability", "get_status", "reserve"])
        self.assertEqual(schemas["check_availability"]["required"], [])
        self.assertEqual(schemas["reserve"]["required"], ["holder", "seats"])
        self.assertEqual(schemas["cancel"]["required"], ["reservation_id"])
        self.assertEqual(schemas["get_status"]["required"], ["reservation_id"])
        self.assertEqual(schemas["reserve"]["properties"]["seats"],
                         {"type": "integer", "minimum": 1})

    def test_named_fixture(self):
        env = ReservationEnvironment()
        for name in ("empty_small", "full", "half_full", "near_full"):
            snapshot = env.named_fixture(name)
            self.assertEqual(snapshot["fixture"], name)
            self.assertEqual(snapshot["capacity"], vendored.get_fixture(name)["capacity"])
            self.assertEqual(snapshot["reservations"],
                             vendored.get_fixture(name)["reservations"])
        with self.assertRaises(KeyError):
            env.named_fixture("nope")
        with self.assertRaises(KeyError):
            env.named_fixture(None)

    def test_adapter_satisfies_the_protocol(self):
        self.assertIsInstance(ReservationEnvironment(), EnvironmentAdapter)


class ProvenanceTests(unittest.TestCase):
    def test_vendored_service_is_byte_identical(self):
        digest = hashlib.sha256(Path(vendored.__file__).read_bytes()).hexdigest().upper()
        self.assertEqual(digest, VENDOR_SHA256)

    def test_provenance_names_the_reviewed_revision(self):
        self.assertIn("79684eb", SERVICE_PROVENANCE)

    def test_vendored_service_matches_external_revision(self):
        present = [p for p in EXTERNAL_SERVICE_CANDIDATES if p.is_file()]
        if not present:
            self.skipTest("external ghostlab-ext-example repo absent")
        digest = hashlib.sha256(present[0].read_bytes()).hexdigest().upper()
        self.assertEqual(
            digest, VENDOR_SHA256,
            "external reservation_service.py drifted from the pin; re-vendor"
            " a reviewed commit (see module docstring)")

    def test_declared_tools_match_the_service_allowlist(self):
        self.assertEqual(set(RESERVATION_TOOL_SCHEMAS), set(vendored.ALLOWED_TOOLS))

    def test_package_is_stdlib_only(self):
        for module in (reservation_env, vendored):
            source = Path(module.__file__).read_text(encoding="utf-8")
            for line in source.splitlines():
                if line.startswith(("import ", "from ")):
                    self.assertNotRegex(
                        line, r"\b(socket|urllib|http\.|requests|httpx)\b", line)


class RegistrationTests(unittest.TestCase):
    def test_explicit_registration(self):
        register = register_reservation_adapters(AdapterRegistry())
        self.assertEqual(register.worlds(), (RESERVATION_WORLD_VERSION,))
        self.assertEqual(register.targets(RESERVATION_WORLD_VERSION), ())
        self.assertIs(register.environment_manifest(RESERVATION_WORLD_VERSION),
                      ENVIRONMENT_MANIFEST)
        self.assertEqual(ENVIRONMENT_MANIFEST.origin, "checked_in")
        self.assertIsInstance(register.environment_factory(RESERVATION_WORLD_VERSION)(),
                              EnvironmentAdapter)
        with self.assertRaises(UnknownTargetError):
            register.target_factory(RESERVATION_WORLD_VERSION, "nope")
        with self.assertRaises(UnknownWorldError):
            register.environment_factory("nope/1")

    def test_default_registry_is_fresh(self):
        register = register_reservation_adapters()
        self.assertIsInstance(register, AdapterRegistry)
        self.assertEqual(register.worlds(), (RESERVATION_WORLD_VERSION,))

    def test_duplicates_need_explicit_replace(self):
        register = register_reservation_adapters(AdapterRegistry())
        with self.assertRaises(RegistrationError):
            register_reservation_adapters(register)
        register.register_environment(RESERVATION_WORLD_VERSION, ReservationEnvironment,
                                      ENVIRONMENT_MANIFEST, replace=True)
        self.assertEqual(register.worlds(), (RESERVATION_WORLD_VERSION,))

    def test_no_code_loading_api_exists(self):
        exposed = {name for name in dir(AdapterRegistry) if not name.startswith("_")}
        loaders = {"load", "import_module", "importlib", "exec", "eval", "from_path",
                   "load_path", "install", "fetch"}
        self.assertEqual(exposed & loaders, set())


class ResetTests(unittest.TestCase):
    def test_valid_reset_matches_the_raw_service(self):
        raw = ReservationService(FIXTURE).observe()
        state = fresh().observe().state
        self.assertEqual(state, {key: value for key, value in raw.items()
                                 if key not in ("faults", "closed")})
        self.assertEqual(fresh().observe().calls, 0)
        self.assertEqual(fresh().observe().trace, [])

    def test_tampered_fixtures_are_refused(self):
        env = ReservationEnvironment()
        snapshot = env.named_fixture(FIXTURE)
        mutated = dict(snapshot)
        mutated["capacity"] = 999
        grown = deepcopy(snapshot)
        grown["reservations"].append({"holder": "mallory", "seats": 99})
        cases = ["nope", {}, {"fixture": "nope"}, mutated, grown,
                 {**snapshot, "extra": 1},
                 {"fixture": FIXTURE, "capacity": snapshot["capacity"]}]
        for bad in cases:
            with self.assertRaises(ValueError, msg=bad):
                env.reset(bad, dict(CLEAN_SCENARIO), actor_id="x")

    def test_bad_scenarios_are_refused(self):
        env = ReservationEnvironment()
        snapshot = env.named_fixture(FIXTURE)
        cases = [{**CLEAN_SCENARIO, "extra": 1},
                 {**CLEAN_SCENARIO, "timeout_after_commit": "yes"},
                 {**CLEAN_SCENARIO, "stale_snapshot": 1},
                 {**CLEAN_SCENARIO, "max_tool_calls": 0},
                 {**CLEAN_SCENARIO, "max_tool_calls": 1001},
                 {**CLEAN_SCENARIO, "max_tool_calls": "20"},
                 {**CLEAN_SCENARIO, 5: True},
                 "nope"]
        for bad in cases:
            with self.assertRaises(ValueError, msg=bad):
                env.reset(snapshot, bad, actor_id="x")

    def test_bad_actors_are_refused(self):
        env = ReservationEnvironment()
        snapshot = env.named_fixture(FIXTURE)
        for bad in ("", None, "x" * 101):
            with self.assertRaises(ValueError, msg=bad):
                env.reset(snapshot, dict(CLEAN_SCENARIO), actor_id=bad)

    def test_scenario_defaults(self):
        env = ReservationEnvironment()
        env.reset(env.named_fixture(FIXTURE), {}, actor_id="probe")
        for _ in range(20):
            env.execute(Action(tool="check_availability", arguments={}))
        with self.assertRaises(ActionBudgetExceeded):
            env.execute(Action(tool="check_availability", arguments={}))

    def test_reset_clears_faults(self):
        env = fresh(scenario={**CLEAN_SCENARIO, "stale_snapshot": True})
        env.reset(env.named_fixture(FIXTURE), dict(CLEAN_SCENARIO), actor_id="probe")
        env.execute(Action(tool="check_availability", arguments={}))
        env.execute(Action(tool="reserve", arguments={"holder": "carol", "seats": 1}))
        live = env.execute(Action(tool="check_availability", arguments={}))
        self.assertFalse(live.result["stale"])

    def test_double_reset_is_clean(self):
        env = fresh()
        env.execute(Action(tool="check_availability", arguments={}))
        env.reset(env.named_fixture(FIXTURE), dict(CLEAN_SCENARIO), actor_id="probe")
        observed = env.observe()
        self.assertEqual((observed.calls, observed.trace), (0, []))


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.env = fresh()

    def test_non_actions_raise_before_touching_state(self):
        with self.assertRaises(TypeError):
            self.env.execute({"tool": "check_availability"})
        self.assertEqual(self.env.observe().calls, 0)

    def test_unknown_tools_are_recorded_world_errors(self):
        before = self.env.observe()
        outcome = self.env.execute(Action(tool="nope", arguments={}))
        self.assertEqual((outcome.status, outcome.error), ("error", "unknown_tool"))
        after = self.env.observe()
        self.assertEqual(after.state, before.state)
        self.assertEqual(after.calls, 1)
        self.assertEqual(after.trace[0]["actor_id"], "qual-probe")

    def test_unknown_args_never_mutate(self):
        before = self.env.observe()
        outcome = self.env.execute(Action(
            tool="reserve", arguments={"holder": "x", "seats": 1, "extra": 1}))
        self.assertEqual((outcome.status, outcome.error), ("error", "unknown_arg"))
        after = self.env.observe()
        self.assertEqual(after.state, before.state)
        self.assertEqual(after.state["revision"], 0)

    def test_bad_args_are_recorded_world_errors(self):
        cases = [({"holder": "x"}, "invalid_args"),
                 ({"holder": "x", "seats": "two"}, "invalid_args"),
                 ({"holder": "  ", "seats": 1}, "invalid_args")]
        for arguments, code in cases:
            outcome = self.env.execute(Action(tool="reserve", arguments=arguments))
            self.assertEqual((outcome.status, outcome.error), ("error", code), arguments)
        self.assertEqual(self.env.observe().state["used"], 5)
        self.assertEqual(self.env.observe().state["revision"], 0)

    def test_capacity_rejections_leave_state_untouched(self):
        env = fresh("near_full")
        outcome = env.execute(Action(tool="reserve", arguments={"holder": "mallory",
                                                                "seats": 2}))
        self.assertEqual((outcome.status, outcome.error), ("error", "capacity_exceeded"))
        state = env.observe().state
        self.assertEqual((state["used"], state["available"], state["revision"]), (5, 1, 0))

    def test_ok_paths(self):
        seen = self.env.execute(Action(tool="check_availability", arguments={}))
        self.assertEqual(seen.result,
                         {"capacity": 10, "used": 5, "available": 5, "revision": 0,
                          "stale": False})
        booked = self.env.execute(
            Action(tool="reserve", arguments={"holder": "carol", "seats": 2}))
        self.assertEqual(booked.status, "ok")
        self.assertEqual(booked.result["reservation_id"], "R-0003")
        status = self.env.execute(
            Action(tool="get_status", arguments={"reservation_id": "R-0003"}))
        self.assertEqual(status.result,
                         {"id": "R-0003", "holder": "carol", "seats": 2, "status": "active"})
        freed = self.env.execute(Action(tool="cancel", arguments={"reservation_id": "R-0003"}))
        self.assertEqual((freed.status, freed.result["used"]), ("ok", 5))
        missing = self.env.execute(
            Action(tool="get_status", arguments={"reservation_id": "R-9999"}))
        self.assertEqual((missing.status, missing.error), ("error", "not_found"))

    def test_budget_refusals_record_no_event(self):
        env = fresh(scenario={**CLEAN_SCENARIO, "max_tool_calls": 2})
        env.execute(Action(tool="check_availability", arguments={}))
        env.execute(Action(tool="check_availability", arguments={}))
        with self.assertRaises(ActionBudgetExceeded):
            env.execute(Action(tool="check_availability", arguments={}))
        observed = env.observe()
        self.assertEqual((observed.calls, len(observed.trace)), (2, 2))

    def test_observations_are_private_copies(self):
        self.env.execute(Action(tool="check_availability", arguments={}))
        observed = self.env.observe()
        observed.trace.append({})
        observed.state["reservations"].append({})
        observed.state["used"] = 999
        before, after = observed.calls, self.env.observe()
        self.assertEqual(before, 1)
        self.assertEqual(len(after.trace), 1)
        self.assertEqual(after.state["used"], 5)
        self.assertEqual(len(after.state["reservations"]), 2)

    def test_close_ends_the_run(self):
        self.env.close()
        with self.assertRaises(RuntimeError):
            self.env.execute(Action(tool="check_availability", arguments={}))
        with self.assertRaises(RuntimeError):
            self.env.observe()
        self.env.close()
        self.env.reset(self.env.named_fixture(FIXTURE), dict(CLEAN_SCENARIO),
                       actor_id="probe")
        self.assertEqual(self.env.observe().calls, 0)

    def test_close_before_reset_is_safe(self):
        ReservationEnvironment().close()


class FaultTests(unittest.TestCase):
    def test_timeout_commits_then_reports(self):
        env = fresh(scenario={**CLEAN_SCENARIO, "timeout_after_commit": True})
        outcome = env.execute(
            Action(tool="reserve", arguments={"holder": "alice", "seats": 3}))
        self.assertEqual((outcome.status, outcome.error), ("error", "TimeoutAfterCommit"))
        state = env.observe().state
        self.assertEqual(state["used"], 8)
        self.assertEqual(state["reservations"][-1]["id"], "R-0003")
        self.assertEqual(env.faults_fired({**CLEAN_SCENARIO, "timeout_after_commit": True},
                                          env.observe().trace), {TIMEOUT_FAULT})

    def test_naive_retry_double_books(self):
        env = fresh(scenario={**CLEAN_SCENARIO, "timeout_after_commit": True})
        first = env.execute(Action(tool="reserve", arguments={"holder": "carol", "seats": 2}))
        retry = env.execute(Action(tool="reserve", arguments={"holder": "carol", "seats": 2}))
        self.assertEqual(first.error, "TimeoutAfterCommit")
        self.assertEqual(retry.error, "TimeoutAfterCommit")
        observed = env.observe()
        self.assertEqual((observed.state["used"], observed.state["revision"]), (9, 2))
        mine = [record for record in observed.state["reservations"]
                if record["holder"] == "carol"]
        self.assertEqual([record["id"] for record in mine], ["R-0003", "R-0004"])
        self.assertEqual(env.faults_fired({**CLEAN_SCENARIO, "timeout_after_commit": True},
                                          observed.trace), {TIMEOUT_FAULT})

    def test_careful_driver_skips_the_retry(self):
        env = fresh(scenario={**CLEAN_SCENARIO, "timeout_after_commit": True})
        timeout = env.execute(
            Action(tool="reserve", arguments={"holder": "alice", "seats": 3}))
        self.assertEqual(timeout.error, "TimeoutAfterCommit")
        seen = env.execute(Action(tool="check_availability", arguments={}))
        self.assertEqual(seen.result["used"], 8)
        observed = env.observe()
        checks = env.evaluate(trace=observed.trace, state=observed.state)
        self.assertTrue(all(check.passed for check in checks))

    def test_scheduled_but_unfired_faults_are_not_reported(self):
        env = fresh(scenario={**CLEAN_SCENARIO, "timeout_after_commit": True})
        env.execute(Action(tool="check_availability", arguments={}))
        self.assertEqual(env.faults_fired({**CLEAN_SCENARIO, "timeout_after_commit": True},
                                          env.observe().trace), frozenset())

    def test_stale_snapshot_is_flagged_and_detectable(self):
        env = fresh(scenario={**CLEAN_SCENARIO, "stale_snapshot": True})
        first = env.execute(Action(tool="check_availability", arguments={}))
        self.assertFalse(first.result["stale"])
        env.execute(Action(tool="reserve", arguments={"holder": "carol", "seats": 2}))
        stale = env.execute(Action(tool="check_availability", arguments={}))
        self.assertTrue(stale.result["stale"])
        self.assertEqual(stale.result["available"], 5)
        self.assertEqual(env.observe().state["available"], 3)
        self.assertEqual(env.faults_fired({**CLEAN_SCENARIO, "stale_snapshot": True},
                                          env.observe().trace), {STALE_FAULT})

    def test_skipped_capacity_check_overbooks(self):
        env = fresh("near_full", scenario={**CLEAN_SCENARIO, "skip_capacity_check": True})
        forced = env.execute(
            Action(tool="reserve", arguments={"holder": "mallory", "seats": 2}))
        self.assertEqual(forced.status, "ok")
        observed = env.observe()
        self.assertEqual((observed.state["used"], observed.state["capacity"]), (7, 6))
        checks = {check.name: check for check in
                  env.evaluate(trace=observed.trace, state=observed.state)}
        self.assertFalse(checks["capacity_respected"].passed)
        self.assertEqual(checks["capacity_respected"].evidence_event_ids, (1,))
        self.assertEqual(env.faults_fired({**CLEAN_SCENARIO, "skip_capacity_check": True},
                                          observed.trace), {SKIP_FAULT})

    def test_invalid_fault_evidence_is_refused(self):
        env = fresh()
        with self.assertRaises(ValueError):
            env.faults_fired({}, "nope")
        with self.assertRaises(ValueError):
            env.faults_fired({}, [{"no_outcome": True}])


class EvaluateTests(unittest.TestCase):
    def test_clean_run_checks(self):
        result, _ = drive(script_actions())
        checks = {check.name: check for check in ReservationEnvironment().evaluate(
            trace=result.trace, state=result.state)}
        self.assertEqual(set(checks), {"capacity_respected", "records_wellformed",
                                       "used_consistent", "used_nonnegative"})
        self.assertTrue(all(check.passed for check in checks.values()))
        capacity = checks["capacity_respected"]
        self.assertEqual((capacity.observed, capacity.limit, capacity.unit),
                         (5, 10, "seats"))
        self.assertEqual(capacity.evidence_event_ids, (2, 4))
        for name in ("records_wellformed", "used_consistent", "used_nonnegative"):
            self.assertEqual(checks[name].evidence_event_ids, ())

    def test_revision_growth_events(self):
        result, _ = drive(script_actions())
        self.assertEqual(revision_growth_events(result.trace), [2, 4])

    def test_checks_match_the_independent_service_detector(self):
        cases = [(script_actions(), FIXTURE, CLEAN_SCENARIO),
                 ([Action(tool="reserve", arguments={"holder": "mallory", "seats": 2})],
                  "near_full", {**CLEAN_SCENARIO, "skip_capacity_check": True}),
                 ([Action(tool="reserve", arguments={"holder": "alice", "seats": 3}),
                   Action(tool="reserve", arguments={"holder": "alice", "seats": 3})],
                  FIXTURE, {**CLEAN_SCENARIO, "timeout_after_commit": True})]
        for script, fixture, scenario in cases:
            result, _ = drive(script, fixture, scenario)
            service = service_checks(script, fixture, scenario)
            found = {check.name: check.passed for check in ReservationEnvironment().evaluate(
                trace=result.trace, state=result.state)}
            violations = service["violations"]
            self.assertEqual(found, {
                "capacity_respected": "capacity_exceeded" not in violations,
                "used_nonnegative": "negative_used" not in violations,
                "used_consistent": "used_mismatch" not in violations,
                "records_wellformed": not any(
                    value.startswith(("nonpositive_seats:", "bad_status:"))
                    for value in violations),
            }, (fixture, scenario))
            self.assertEqual(service["ok"], all(found.values()), (fixture, scenario))

    def test_evaluate_is_pure_and_deterministic(self):
        result, _ = drive(script_actions())
        first = ReservationEnvironment().evaluate(trace=result.trace, state=result.state)
        second = ReservationEnvironment().evaluate(trace=result.trace, state=result.state)
        self.assertEqual(first, second)

    def test_invalid_evidence_is_refused(self):
        env = ReservationEnvironment()
        with self.assertRaises(ValueError):
            env.evaluate(trace=[], state={})
        with self.assertRaises(ValueError):
            env.evaluate(trace={}, state={})
        result, _ = drive(script_actions())
        tampered = deepcopy(result.state)
        tampered["reservations"][0]["seats"] = "two"
        with self.assertRaises(ValueError):
            env.evaluate(trace=result.trace, state=tampered)
        with self.assertRaises(ValueError):
            env.evaluate(trace=[{"event_id": 1}], state=result.state)


class RunnerTests(unittest.TestCase):
    def test_scripted_run_through_the_generic_runner(self):
        result, target = drive(script_actions())
        self.assertEqual((result.status, result.tool_calls), ("completed", 6))
        self.assertIsNone(result.denial)
        self.assertIsNone(result.stopped)
        self.assertEqual([event["event_id"] for event in result.trace], [1, 2, 3, 4, 5, 6])
        self.assertEqual(result.state["used"], 5)
        self.assertEqual(target.closed_with, "finished")
        self.assertEqual([action["status"] for action in target.info().actions],
                         ["ok", "ok", "ok", "ok", "ok", "error"])

    def test_run_isolation(self):
        first, second = fresh(), fresh()
        for action in script_actions():
            first.execute(action)
        self.assertEqual(first.observe().calls, 6)
        self.assertEqual(second.observe().calls, 0)
        first.reset(first.named_fixture(FIXTURE), dict(CLEAN_SCENARIO), actor_id="probe")
        self.assertEqual(first.observe().calls, 0)
        self.assertEqual(second.observe().calls, 0)

    def test_cap_bounds_runner_attempts(self):
        env = fresh()
        target = ScriptTarget(script_actions())
        result = run_attempt(env, target, TargetTask(task_id="p2", params={}),
                             authorize=lambda action: None, cap=2,
                             cooperative_stop=False)
        self.assertEqual((result.status, result.tool_calls), ("budget_exhausted", 2))
        self.assertEqual(target.closed_with, "budget_exhausted")

    def test_denial_wins_without_touching_the_world(self):
        env = fresh()
        target = ScriptTarget(script_actions())
        seen = []
        denial = object()
        result = run_attempt(
            env, target, TargetTask(task_id="p2", params={}), cap=20,
            authorize=lambda action: seen.append(action) or (
                None if len(seen) == 1 else denial),
            cooperative_stop=False)
        self.assertIs(result.denial, denial)
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(len(result.trace), 1)
        self.assertEqual(target.closed_with, "denied")


class DivergenceReplayTests(unittest.TestCase):
    def test_timeout_diverges_at_event_two(self):
        script = [Action(tool="check_availability", arguments={}),
                  Action(tool="reserve", arguments={"holder": "alice", "seats": 1})]
        baseline, _ = drive(script)
        faulted, _ = drive(script, scenario={**CLEAN_SCENARIO, "timeout_after_commit": True})
        self.assertEqual(baseline.trace[0], faulted.trace[0])
        divergence = first_divergence(baseline.trace, faulted.trace)
        self.assertEqual(divergence["event_id"], 2)
        self.assertIn("result", divergence["baseline"]["outcome"])
        self.assertEqual(divergence["faulted"]["outcome"]["error"], "TimeoutAfterCommit")

    def test_stale_snapshot_diverges_at_event_three(self):
        script = [Action(tool="check_availability", arguments={}),
                  Action(tool="reserve", arguments={"holder": "carol", "seats": 1}),
                  Action(tool="check_availability", arguments={})]
        baseline, _ = drive(script)
        faulted, _ = drive(script, scenario={**CLEAN_SCENARIO, "stale_snapshot": True})
        divergence = first_divergence(baseline.trace, faulted.trace)
        self.assertEqual(divergence["event_id"], 3)
        self.assertFalse(divergence["baseline"]["outcome"]["result"]["stale"])
        self.assertTrue(divergence["faulted"]["outcome"]["result"]["stale"])

    def test_replay_reproduces_recorded_runs_without_snapshots(self):
        scripts = [script_actions(),
                   [Action(tool="check_availability", arguments={}),
                    Action(tool="reserve", arguments={"holder": "alice", "seats": 1})]]
        scenarios = [CLEAN_SCENARIO, {**CLEAN_SCENARIO, "timeout_after_commit": True}]
        for script in scripts:
            for scenario in scenarios:
                result, _ = drive(script, scenario=scenario)
                replayed = replay_trace(fresh(scenario=scenario), result.trace)
                self.assertEqual(replayed.trace, result.trace)
                self.assertEqual(replayed.state, result.state)
                self.assertEqual(replayed.calls, result.tool_calls)

    def test_snapshots_are_honestly_unsupported(self):
        env = fresh()
        with self.assertRaises(UnsupportedOperation):
            env.snapshot()
        with self.assertRaises(UnsupportedOperation):
            env.restore({})


if __name__ == "__main__":
    unittest.main()
