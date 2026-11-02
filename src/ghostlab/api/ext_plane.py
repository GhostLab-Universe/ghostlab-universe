"""Synchronous plane over the operator extension registry (P6).

Every method is a thin validated call into the P1/P2/P4/P5 libraries: no
world logic, no model calls, no network, no jobs. Worlds served here are
synthetic local fixtures, so probe actor ids are caller-declared test
identities (the demo needs customer and stranger actors); the API session
still scopes the call itself, and nothing here touches production data.

Budget bounds are tighter than the libraries (attempts, caps, repeats):
the API is shared and synchronous.
"""

import json

from ghostlab import capsule as refund_capsule
from ghostlab.adapters import register_refund_adapters
from ghostlab.adapters.reservation_env import register_reservation_adapters
from ghostlab.explore.harness import AttemptSpec, run_campaign
from ghostlab.extensions.environment import Action
from ghostlab.extensions.qualify import ProbeScript, ScriptTarget, qualify_environment
from ghostlab.extensions.registry import AdapterManifest, AdapterRegistry
from ghostlab.extensions.runner import run_attempt
from ghostlab.extensions.target import TargetTask
from ghostlab.replay import (allow_tools, assess_fidelity, inspect_capsule,
                             minimize_faults, replay_recorded, retest_fresh)
from ghostlab.replay.minimize_ext import RunEvidence

MAX_ENVELOPE_BYTES = refund_capsule.MAX_CAPSULE_BYTES


class ExtTooLarge(ValueError):
    """A request body exceeds the shared synchronous bound."""


class ExtBaselineNotReproduced(ValueError):
    """The baseline run did not show the selected failure; nothing minimized."""


def default_registry() -> AdapterRegistry:
    """Fresh registry with the reviewed local adapters registered."""
    registry = AdapterRegistry()
    register_refund_adapters(registry)
    register_reservation_adapters(registry)
    return registry


def _actions(script) -> list:
    try:
        return [Action(tool=item["tool"], arguments=dict(item["arguments"]))
                for item in script]
    except (KeyError, TypeError, ValueError):
        raise ValueError("Invalid probe script") from None


def _jsonable(value):
    """Deep plain-JSON copy: tuples become lists, mappings become dicts."""
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _task(task) -> TargetTask:
    return TargetTask(task_id=task["task_id"], params=dict(task["params"]))


class ExtensionPlane:
    """Stateless extension operations over one operator registry."""

    def __init__(self, registry: AdapterRegistry | None = None):
        self.registry = registry if registry is not None else default_registry()

    def _describe(self, world_version: str):
        env = self.registry.environment_factory(world_version)()
        try:
            return env.describe()
        finally:
            env.close()

    def list_environments(self) -> list:
        summaries = []
        for world in self.registry.worlds():
            detail = self.describe_environment(world)
            summaries.append({key: detail[key] for key in (
                "world_version", "environment_id", "environment_version",
                "source", "tools", "faults", "invariants", "fixtures",
                "capabilities", "targets")})
        return summaries

    def describe_environment(self, world_version: str) -> dict:
        description = self._describe(world_version)
        manifest = self.registry.environment_manifest(world_version)
        return {
            "world_version": description.world_version,
            "environment_id": description.environment_id,
            "environment_version": description.environment_version,
            "source": description.source,
            "tools": [tool.name for tool in description.tools],
            "faults": [fault.fault_id for fault in description.faults],
            "invariants": list(description.invariants),
            "fixtures": list(description.fixtures),
            "capabilities": dict(description.capabilities),
            "targets": list(self.registry.targets(world_version)),
            "tool_specs": [{"name": tool.name, "effect": tool.effect,
                            "parameters": dict(tool.parameters),
                            "description": tool.description}
                           for tool in description.tools],
            "fault_specs": [{"fault_id": fault.fault_id,
                             "parameters": [{"name": param.name, "type": param.type,
                                             "description": param.description}
                                            for param in fault.parameters],
                             "trigger_points": list(fault.trigger_points),
                             "description": fault.description}
                            for fault in description.faults],
            "package": manifest.package, "package_version": manifest.version,
        }

    def fixture_snapshot(self, world_version: str, name: str) -> dict:
        env = self.registry.environment_factory(world_version)()
        try:
            snapshot = env.named_fixture(name)
        except KeyError:
            raise LookupError(f"Unknown fixture {name!r}") from None
        finally:
            env.close()
        return {"world_version": world_version, "name": name,
                "snapshot": dict(snapshot)}

    def _authorize(self, world_version: str, allow: list | None):
        if allow is None:
            allow = [tool.name for tool in self._describe(world_version).tools]
        return allow_tools(*allow)

    def _target_factory(self, world_version: str, target: dict):
        if target["kind"] == "registered":
            return self.registry.target_factory(world_version, target["id"])
        script = _actions(target["actions"])
        return lambda: ScriptTarget(list(script), adapter_id="api-script")

    def qualify(self, *, world_version: str, fixture: str, scenario: dict,
                script: list, task_id: str) -> dict:
        probe = ProbeScript(fixture=fixture, scenario=dict(scenario),
                            script=_actions(script), task_id=task_id)
        report = qualify_environment(
            world_version=world_version,
            env_factory=self.registry.environment_factory(world_version),
            manifest=self.registry.environment_manifest(world_version), probe=probe)
        return report.to_dict()

    def fidelity(self, *, world_version: str, fixture: str, scenario: dict,
                 actor_id: str, script: list, repeats: int, expected_trace,
                 expected_state, declared_tested, declared_approximations,
                 declared_omissions) -> dict:
        factory = self.registry.environment_factory(world_version)
        probe = factory()
        try:
            snapshot = probe.named_fixture(fixture)
        finally:
            probe.close()
        report = assess_fidelity(
            env_factory=factory, fixture=snapshot, scenario=dict(scenario),
            actor_id=actor_id, script=_actions(script), repeats=repeats,
            expected_trace=expected_trace, expected_state=expected_state,
            declared_tested=list(declared_tested),
            declared_approximations=list(declared_approximations),
            declared_omissions=list(declared_omissions))
        return report.to_dict()

    def campaign(self, *, world_version: str, attempts: list,
                 allow_tools: list | None) -> dict:
        factory = self.registry.environment_factory(world_version)
        authorize = self._authorize(world_version, allow_tools)
        evaluator = factory()
        try:
            def assess(trace, state):
                verdicts = {check.name: check.passed
                            for check in evaluator.evaluate(trace=trace, state=state)}
                failed = sorted(name for name, hit in verdicts.items() if hit is False)
                return {"failed_rules": failed, "checks": verdicts}

            specs = [AttemptSpec(
                name=item["name"], arm=item["arm"], fixture_name=item["fixture_name"],
                scenario=dict(item["scenario"]), actor_id=item["actor_id"],
                task=_task(item["task"]),
                target_factory=self._target_factory(world_version, item["target"]),
                cap=item["cap"]) for item in attempts]
            report = run_campaign(specs, factory, authorize=authorize, assess=assess,
                                  max_attempts=20)
        finally:
            evaluator.close()
        return _jsonable({"attempts": [dict(attempt) for attempt in report.attempts],
                            "coverage": dict(report.coverage),
                            "bounds": dict(report.bounds),
                            "arms": dict(report.arms)})

    def inspect(self, envelope: dict) -> dict:
        if len(json.dumps(envelope).encode()) > MAX_ENVELOPE_BYTES:
            raise ExtTooLarge("Capsule envelope too large")
        return inspect_capsule(envelope).to_dict()

    def replay(self, *, world_version: str, fixture: dict, scenario: dict,
               actor_id: str, trace: list, state: dict, checks: dict) -> dict:
        return replay_recorded(
            registry=self.registry, world_version=world_version, fixture=fixture,
            scenario=scenario, actor_id=actor_id, recorded_trace=trace,
            recorded_state=state, recorded_checks=checks).to_dict()

    def retest(self, *, world_version: str, target: dict, fixture: dict,
               scenario: dict, actor_id: str, task: dict, allow_tools: list | None,
               cap: int) -> dict:
        registry = self.registry
        target_id = target["id"] if target["kind"] == "registered" else "api-script"
        if target["kind"] == "script":
            script = _actions(target["actions"])
            registry = AdapterRegistry()
            registry.register_environment(
                world_version, self.registry.environment_factory(world_version),
                self.registry.environment_manifest(world_version))
            registry.register_target(
                world_version, target_id,
                lambda: ScriptTarget(list(script), adapter_id=target_id),
                AdapterManifest(package="ghostlab.api.ext_plane",
                                version="api-script/1", origin="checked_in",
                                description="Inline API probe script"))
        return retest_fresh(
            registry=registry, world_version=world_version, target_id=target_id,
            fixture=fixture, scenario=scenario, actor_id=actor_id, task=_task(task),
            authorize=self._authorize(world_version, allow_tools), cap=cap).to_dict()

    def minimize(self, *, world_version: str, target: dict, fixture: dict,
                 scenario_template: dict, fault_keys: list, actor_id: str, task: dict,
                 allow_tools: list | None, cap: int, invariant: str, max_runs: int,
                 deadline_ms: int) -> dict:
        factory = self.registry.environment_factory(world_version)
        authorize = self._authorize(world_version, allow_tools)
        parsed_task = _task(task)
        keys = list(fault_keys)

        def run(subset):
            scenario = dict(scenario_template)
            scenario.update({key: key in subset for key in keys})
            env = factory()
            script_target = self._target_factory(world_version, target)()
            env.reset(dict(fixture), scenario, actor_id=actor_id)
            attempt = run_attempt(env, script_target, parsed_task, authorize=authorize,
                                  cap=cap)
            evaluator = factory()
            try:
                checks = evaluator.evaluate(trace=attempt.trace, state=attempt.state)
            finally:
                evaluator.close()
            return RunEvidence(status=attempt.status, checks=checks,
                               tool_calls=attempt.tool_calls)

        try:
            report = minimize_faults(run=run, faults=keys, invariant=invariant,
                                       max_runs=max_runs, deadline_ms=deadline_ms)
        except ValueError as error:
            if str(error) == "selected failure cannot be reproduced":
                raise ExtBaselineNotReproduced(
                    "The baseline run did not reproduce the selected failure") from None
            raise
        return report.to_dict()
