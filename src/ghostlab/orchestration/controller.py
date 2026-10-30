"""M05 campaign controller: an explicit LangGraph loop (``ghostlab-orchestration/2``).

plan → propose (Explorer, M01 via M03 context) → validate → execute (baseline and
fork, M06-authorized world calls) → verify (deterministic findings) → remember
(M04 memory) → [request_repair → await_approval ⟲ → consume → execute_repair] →
plan … → end.

Trust rules:

- Authority comes from the ``TrustedSession`` given to the constructor (M07 will
  derive it from an authenticated session), never from graph state, a resume
  payload or model output. Every entry re-checks that the campaign belongs to
  the session's project and principal.
- Graph state holds plain JSON only (contract dumps); nodes re-validate through
  M00 contracts at each boundary.
- An approval is never granted by resuming. ``await_approval`` ignores the resume
  value and reads the stored approval; only a successful single-use
  ``ApprovalService.consume`` (its own checkpointed node) authorizes the repair.
- Resuming a checkpoint is graph resume: it can repeat model calls for an
  unfinished node. It is not Failure Capsule replay, which makes no model calls.
- A world runs only through a driver in ``Services.worlds`` and only past the
  runnable gate, checked on start, resume, recovery and before each execution.
  The world comes from the checkpointed spec, never from a proposal.
- The world's fixture is pinned at start. Execution, verification and capsule
  export use that record, an approval binds its digest, and resume or recovery
  under a different fixture is refused before an approval can be consumed.
- The execution mode comes from the driver and M06, and is recorded with the
  run's evidence. Lab mode relaxes only the business rules under test: identity,
  project, ownership, tool validation and budgets hold in every run.
"""

import hashlib
import json
import tempfile
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ghostlab.budgets import BudgetAccountant, BudgetExhausted, utc_timestamp
from ghostlab.context import ContextCompiler, ContextError
from ghostlab.contracts import (BudgetLimits, CampaignRecord, CampaignSpec, ExperimentEvent,
                                Principal, Provenance, Ref)
from ghostlab.contracts.primitives import parse_timestamp
from ghostlab.core import Fixture
from ghostlab.memory import MemoryPipeline
from ghostlab.memory.pipeline import Observation as MemoryObservation
from ghostlab.policy import (ApprovalDenied, ApprovalService, PolicyDenied, PrincipalBinding,
                             StaticWorldBinding, WorldActorBinding, action_digest,
                             authorize_capability, authorize_world, ceiling_for,
                             check_within_ceiling, delegate, root_scope)
from ghostlab.policy.authorization import deny
from ghostlab.providers import ProviderAdapter, Runtime
from ghostlab.skills import SkillRegistry
from ghostlab.storage.interfaces import ApprovalStore, Conflict, MemoryStore, NotFound
from ghostlab.worlds.journey import JourneyFixture

from . import tasks as board
from .engine import execute_run
from .inference import (ExplorerClient, LiveInferenceAuthorization, LiveInferenceRefused,
                        require_inference_allowed)
from .worlds import DEFAULT_WORLDS, baseline_scenario, canonical

MODEL_RESOURCES = ("model_calls", "output_tokens", "input_tokens")

ORCHESTRATION_VERSION = "ghostlab-orchestration/2"


class CampaignExists(Exception):
    """A campaign id may be started once per project; it is never reused or taken over."""


class CampaignSettingsChanged(Exception):
    """The current world settings differ from the ones this campaign recorded."""


ROUTES = ("plan", "propose", "validate", "execute", "verify", "remember", "request_repair",
          "await_approval", "consume", "execute_repair")


def ledger_key(project_id: str, campaign_id: str) -> str:
    """Key for a campaign's checkpoints and budget rows: ``<project>:<campaign>``.

    Identifiers may contain ":", so a project id with one would be ambiguous. Those
    keys hash the pair instead. A raw key always contains ":" and a hashed key never
    does, so the two forms can't collide, and other projects keep their existing keys.
    """
    if ":" not in project_id:
        return f"{project_id}:{campaign_id}"
    return "h." + hashlib.sha256(f"{project_id}\x1f{campaign_id}".encode()).hexdigest()[:32]


@dataclass(frozen=True)
class TrustedSession:
    """Principal and project from the authenticated session (M07); never from a body."""

    principal: Principal
    project_id: str


@dataclass
class Services:
    memory_store: MemoryStore
    approval_store: ApprovalStore
    approvals: ApprovalService
    ledger: object                    # M04 BudgetLedger
    artifacts: object                 # M04 ArtifactStore
    skills: SkillRegistry
    binding: PrincipalBinding
    adapter_factory: Callable[[Runtime], ProviderAdapter]
    runtime: Runtime
    clock: Callable[[], datetime]
    live: LiveInferenceAuthorization | None = None
    engine: Callable = execute_run    # injectable for spies; must keep execute_run semantics
    fixture: Fixture = field(default_factory=Fixture)
    on_node: Callable[[str, dict], None] = lambda name, state: None  # test fault injection
    worlds: Mapping = DEFAULT_WORLDS  # explicit: world_version -> checked-in driver
    world_binding: WorldActorBinding = field(default_factory=lambda: StaticWorldBinding({}))
    journey_fixture: JourneyFixture = field(default_factory=JourneyFixture)


class CampaignState(TypedDict, total=False):
    campaign_id: str
    project_id: str
    principal_id: str
    trace_id: str
    spec: dict
    limits: dict
    status: str
    outcome: str | None
    iteration: int
    max_iterations: int
    started_at: str
    tasks: list
    proposal: dict | None
    experiment_id: str | None
    experiments: dict
    findings: list
    memory_ids: list
    approval: dict | None
    repair: dict | None
    events: list
    errors: list
    next: str
    provider: dict          # provider, endpoint and model pinned at start
    fixture: dict           # world fixture pinned at start
    fixture_sha256: str     # bound into any approved action
    task_baselines: dict    # ledger totals when each model task was planned


class CampaignController:
    def __init__(self, session: TrustedSession, services: Services, *, saver,
                 max_iterations: int = 2, approval_ttl: timedelta = timedelta(hours=1),
                 cancel: threading.Event | None = None):
        self.session, self.s, self.saver = session, services, saver
        self.root = root_scope(session.project_id, session.principal)
        self.max_iterations, self.approval_ttl = max_iterations, approval_ttl
        self.cancel = cancel or threading.Event()
        self.explorer = ExplorerClient(ContextCompiler(services.skills, services.memory_store),
                                       services.adapter_factory, services.runtime)
        self.memory = MemoryPipeline(services.memory_store)
        self.graph = self._build().compile(checkpointer=saver)

    # -- public API -----------------------------------------------------------------

    def start(self, campaign_id: str, spec: CampaignSpec) -> dict:
        driver = self._driver(spec.world_version)  # refused before any checkpoint exists
        if self.graph.get_state(self._config(campaign_id)).values:
            raise CampaignExists(f"campaign {campaign_id} already exists in this project")
        within = check_within_ceiling("supervisor", spec.budget)
        if not within.allowed:
            raise PolicyDenied(within)
        config = self.s.adapter_factory(self.s.runtime).config
        limits = require_inference_allowed(self.s.runtime, config, spec.budget, self.s.live)
        now = utc_timestamp(self.s.clock())
        fixture = driver.snapshot(driver.current_fixture(self.s))
        state: CampaignState = {
            "campaign_id": campaign_id, "project_id": self.session.project_id,
            "principal_id": self.session.principal.principal_id,
            "trace_id": hashlib.sha256(campaign_id.encode()).hexdigest()[:32],
            "spec": spec.model_dump(mode="json"), "limits": limits.model_dump(mode="json"),
            "status": "running", "outcome": None, "iteration": 0,
            "max_iterations": self.max_iterations, "started_at": now, "tasks": [],
            "proposal": None, "experiment_id": None, "experiments": {}, "findings": [],
            "memory_ids": [], "approval": None, "repair": None, "events": [], "errors": [],
            "next": "plan", "task_baselines": {}, "fixture": fixture,
            "fixture_sha256": hashlib.sha256(canonical(fixture)).hexdigest(),
            "provider": {"provider": config.provider, "endpoint": config.base_url,
                         "model": config.model}}
        state["events"] = self._event(state, "campaign_created", "Campaign created")
        return self._invoke(state, campaign_id)

    def resume(self, campaign_id: str) -> dict:
        """Re-check a paused campaign. The resume payload grants nothing."""
        self._check_owner(campaign_id)
        if not self.graph.get_state(self._config(campaign_id)).next:
            return self._snapshot(campaign_id)  # finished: nothing to resume
        self._check_settings(self._snapshot(campaign_id))
        self._inference_limits(self._snapshot(campaign_id))  # re-authorize before any call
        return self._invoke(Command(resume="check"), campaign_id)

    def recover(self, campaign_id: str) -> dict:
        """Continue after a crash from the last checkpoint (graph resume, not replay).

        Completed nodes are not re-run; the interrupted node runs again.
        """
        self._check_owner(campaign_id)
        self._check_settings(self._snapshot(campaign_id))
        self._inference_limits(self._snapshot(campaign_id))  # re-authorize before any call
        return self._invoke(None, campaign_id)

    def state(self, campaign_id: str) -> dict:
        self._check_owner(campaign_id)
        return self._snapshot(campaign_id)

    def _snapshot(self, campaign_id: str) -> dict:
        snapshot = self.graph.get_state(self._config(campaign_id))
        return {**snapshot.values, "paused": bool(snapshot.next)}

    def record(self, campaign_id: str) -> CampaignRecord:
        state = self.state(campaign_id)
        status = "awaiting_approval" if state["paused"] else state["status"]
        return self._world_driver(state).record_model.model_validate({
            "schema_version": 1, "campaign_id": campaign_id, "project_id": state["project_id"],
            "created_by": self.session.principal.model_dump(), "spec": state["spec"],
            "status": status, "usage": self._accountant(state).usage(
                wall_clock_ms=self._elapsed_ms(state)).model_dump(mode="json"),
            "trace_id": state["trace_id"], "created_at": state["started_at"],
            "updated_at": utc_timestamp(self.s.clock())})

    def export_capsule(self, campaign_id: str, *, run: str = "fork",
                       experiment_id: str | None = None) -> dict:
        """Store a Failure Capsule for one recorded run, in this project only."""
        self._check_owner(campaign_id)
        state = self._snapshot(campaign_id)
        driver, scope = self._world_driver(state), self._supervisor(state)
        if not (allowed := authorize_capability(
                scope, "export_capsule", project_id=self.session.project_id)).allowed:
            raise PolicyDenied(allowed)
        experiment_id = experiment_id or list(state["experiments"])[-1]
        ref, scenario, execution = self._recorded_run(state, experiment_id, run)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capsule.json"
            driver.capsule(self._fixture(state), scenario, self._load_json(ref), execution, path)
            data = path.read_bytes()
        return self._store_bytes(f"{experiment_id}-{run}-capsule", data,
                                 {"kind": "run", "id": ref["id"], "sha256": None})

    def replay_capsule(self, campaign_id: str, *, run: str = "fork",
                       experiment_id: str | None = None) -> dict:
        """Re-execute a stored capsule's recorded actions. Makes no model call."""
        self._check_owner(campaign_id)
        state = self._snapshot(campaign_id)
        driver, scope = self._world_driver(state), self._supervisor(state)
        if not (allowed := authorize_capability(
                scope, "read_evidence", project_id=self.session.project_id)).allowed:
            raise PolicyDenied(allowed)
        experiment_id = experiment_id or list(state["experiments"])[-1]
        view = self._artifacts()
        stored = view.ref(view.artifact_id(f"{experiment_id}-{run}-capsule"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capsule.json"
            path.write_bytes(view.read(stored))
            return driver.replay(path)

    def _recorded_run(self, state, experiment_id, run):
        experiment = state["experiments"].get(experiment_id)
        if experiment is None:
            raise NotFound("No such experiment in this campaign")
        scenario = experiment["proposal"]["scenario"]
        if run == "fork":
            return experiment["fork"], scenario, experiment.get("execution")
        if run == "baseline":
            return (experiment["baseline"], baseline_scenario(self._world_driver(state),
                                                              experiment["proposal"]),
                    experiment.get("execution"))
        repair = state.get("repair") or {}
        if run == "repaired" and repair.get("run"):
            return repair["run"], scenario, repair.get("execution")
        raise NotFound("No such recorded run")

    # -- graph ------------------------------------------------------------------------

    def _build(self) -> StateGraph:
        graph = StateGraph(CampaignState)
        for name in ROUTES:
            graph.add_node(name, self._wrap(name, getattr(self, f"_{name}")))
        graph.add_edge(START, "plan")
        routes = {name: name for name in ROUTES} | {"end": END}
        for name in ROUTES:
            graph.add_conditional_edges(name, lambda state: state["next"], routes)
        return graph

    def _wrap(self, name, node):
        def run(state):
            self.s.on_node(name, state)
            return node(state)
        return run

    def _invoke(self, payload, campaign_id):
        self.graph.invoke(payload, self._config(campaign_id), durability="sync")
        return self._snapshot(campaign_id)

    def _config(self, campaign_id):
        # Threads are namespaced by project, so equal campaign ids never collide.
        return {"configurable": {"thread_id": ledger_key(self.session.project_id, campaign_id)}}

    def _driver(self, world_version: str):
        """The runnable gate: the world must be implemented and have a checked-in driver."""
        if not (gate := authorize_world(world_version)).allowed:
            raise PolicyDenied(gate)
        driver = self.s.worlds.get(world_version)
        if driver is None:
            raise PolicyDenied(deny("world_not_runnable",
                                    f"{world_version!r} has no checked-in implementation"))
        return driver

    def _world_driver(self, state):
        return self._driver(state["spec"]["world_version"])

    def _fixture(self, state):
        """The fixture this campaign's evidence came from, never the current setting."""
        driver, snapshot = self._world_driver(state), state.get("fixture")
        if snapshot is None:
            if driver.world_version != "refund-world/1":
                raise CampaignSettingsChanged("this campaign has no recorded fixture")
            return driver.current_fixture(self.s)  # a checkpoint from before pinning
        if hashlib.sha256(canonical(snapshot)).hexdigest() != state["fixture_sha256"]:
            raise CampaignSettingsChanged("the recorded fixture failed its digest")
        return driver.restore(snapshot)

    def _settings_problem(self, state) -> str | None:
        driver, snapshot = self._world_driver(state), state.get("fixture")
        if snapshot is None:
            return (None if driver.world_version == "refund-world/1"
                    else "this campaign has no recorded fixture")
        if hashlib.sha256(canonical(snapshot)).hexdigest() != state["fixture_sha256"]:
            return "the recorded fixture failed its digest"
        if driver.snapshot(driver.current_fixture(self.s)) != snapshot:
            return "the world fixture changed since this campaign started"
        action = (state.get("approval") or {}).get("action") or {}
        if action.get("fixture_sha256") not in (None, state["fixture_sha256"]):
            return "the approved action names a different fixture"
        return None

    def _check_settings(self, state):
        """The runnable gate plus the pinned settings, before any node can run."""
        if (problem := self._settings_problem(state)) is not None:
            raise CampaignSettingsChanged(problem)

    def _supervisor(self, state):
        return self._world_driver(state).supervisor(self.session.project_id,
                                                    self.session.principal)

    def _check_owner(self, campaign_id):
        values = self.graph.get_state(self._config(campaign_id)).values
        if not values:
            raise PolicyDenied(deny("cross_project", "no such campaign in this session"))
        if (values.get("project_id") != self.session.project_id
                or values.get("principal_id") != self.session.principal.principal_id):
            raise PolicyDenied(deny("cross_project", "campaign belongs to another session"))

    # -- helpers ----------------------------------------------------------------------

    def _now(self) -> str:
        return utc_timestamp(self.s.clock())

    def _elapsed_ms(self, state) -> int:
        delta = self.s.clock() - parse_timestamp(state["started_at"])
        return max(0, int(delta.total_seconds() * 1000))

    def _limits(self, state) -> BudgetLimits:
        return BudgetLimits.model_validate(state["limits"])

    def _inference_limits(self, state) -> BudgetLimits:
        """Re-run the inference gate for the current runtime; return effective limits.

        Called by ``resume``/``recover`` and by every node before inference, so a
        restart can never swap in an unauthorized transport, another provider,
        endpoint or model, or a larger call cap than the one in force.
        """
        config = self.s.adapter_factory(self.s.runtime).config
        current = {"provider": config.provider, "endpoint": config.base_url,
                   "model": config.model}
        if state.get("provider") != current:
            raise LiveInferenceRefused("provider, endpoint or model changed since start")
        stored = self._limits(state)
        allowed = require_inference_allowed(self.s.runtime, config, stored, self.s.live)
        return stored.model_validate({**stored.model_dump(), "max_model_calls": min(
            stored.max_model_calls, allowed.max_model_calls)})

    def _accountant(self, state, limits: BudgetLimits | None = None) -> BudgetAccountant:
        # Ledger rows are namespaced by project too (M04's ledger has no project column).
        return BudgetAccountant(self.s.ledger,
                                ledger_key(state["project_id"], state["campaign_id"]),
                                limits or self._limits(state), clock=self.s.clock)

    def _deadline_passed(self, state) -> bool:
        return self._elapsed_ms(state) >= self._limits(state).max_wall_clock_ms

    def _event(self, state, kind, summary, *, task_id=None, run_id=None, refs=()):
        event = ExperimentEvent.model_validate({
            "schema_version": 1, "campaign_id": state["campaign_id"],
            "sequence": len(state.get("events", [])) + 1, "kind": kind, "task_id": task_id,
            "run_id": run_id, "trace_id": state["trace_id"], "occurred_at": self._now(),
            "refs": list(refs), "summary": summary[:2000]})
        return [*state.get("events", []), event.model_dump(mode="json")]

    def _prefix(self, state) -> str:
        return f"{state['campaign_id']}-i{state['iteration']}"

    def _stop(self, state, status, outcome, *, error=None, tasks=None, kind=None):
        tasks = board.cancel_open(tasks if tasks is not None else state["tasks"]) \
            if status in {"cancelled", "budget_exhausted", "failed"} else \
            (tasks if tasks is not None else state["tasks"])
        update = {"status": status, "outcome": outcome, "next": "end", "tasks": tasks}
        events = self._event({**state, "tasks": tasks}, kind or "campaign_status_changed",
                             f"Campaign {status}: {outcome}")
        update["events"] = events
        if error is not None:
            update["errors"] = [*state.get("errors", []), error]
        return update

    @staticmethod
    def _error(code, message, *, retryable=False, side_effect="none"):
        return {"code": code, "message": message[:1000], "retryable": retryable,
                "side_effect": side_effect, "ref": None}

    def _guard(self, state):
        """Stop on cancellation, wall-clock deadline or session mismatch."""
        if (state["project_id"] != self.session.project_id
                or state["principal_id"] != self.session.principal.principal_id):
            raise PolicyDenied(deny("cross_project", "campaign belongs to another session"))
        if self.cancel.is_set():
            return self._stop(state, "cancelled", "cancelled_by_request",
                              error=self._error("cancelled", "Campaign cancelled"))
        if self._elapsed_ms(state) >= self._limits(state).max_wall_clock_ms:
            return self._stop(state, "budget_exhausted", "wall_clock_ms",
                              error=self._error("deadline_exceeded", "Campaign deadline reached"),
                              kind="budget_exhausted")
        return None

    def _exhausted(self, state, resource, tasks=None):
        return self._stop(state, "budget_exhausted", resource, tasks=tasks, kind="budget_exhausted",
                          error=self._error("budget_exhausted", f"{resource} budget exhausted"))

    def _artifacts(self):
        """This session's project view. Ownership is enforced by M04, not by the id."""
        return self.s.artifacts.for_project(self.root)

    def _store_bytes(self, name, data, kind_ref) -> dict:
        view = self._artifacts()
        provenance = Provenance(source_kind="controller", source_ref=kind_ref,
                                observed_at=self._now(), trust="trusted")
        try:
            ref = view.put(data, name=name, media_type="application/json",
                           provenance=provenance, created_at=self._now())
        except Conflict:
            ref = view.ref(view.artifact_id(name))  # re-run after a crash: same content id
            if ref.sha256 != hashlib.sha256(data).hexdigest():
                raise
        return {"kind": "artifact", "id": ref.artifact_id, "sha256": ref.sha256}

    def _store_json(self, name, value, kind_ref) -> dict:
        return self._store_bytes(name, canonical(value), kind_ref)

    def _load_json(self, ref: dict):
        view = self._artifacts()
        try:
            return json.loads(view.read(view.ref(ref["id"])))
        except NotFound:  # stored before ownership; the id came from this owned checkpoint
            return json.loads(self.s.artifacts.read(self.s.artifacts.ref(ref["id"])))

    def _stop_reason(self, state) -> str | None:
        if self.cancel.is_set():
            return "cancelled"
        return "wall_clock_ms" if self._deadline_passed(state) else None

    def _run_pair(self, state, task_id, runs):
        """Execute run requests in parallel within campaign limits."""
        driver, fixture = self._world_driver(state), self._fixture(state)
        target = driver.target(self.session.project_id, self.session.principal)
        accountant = self._accountant(state)
        task_cap = ceiling_for("target").max_tool_calls * len(runs)
        reservations, settled = [], False
        try:
            for run in runs:
                reservations.append(accountant.reserve(
                    task_id, "tool_calls", run.cap, task_limit=task_cap))
            workers = max(1, min(self._limits(state).max_parallel_runs, len(runs)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                outcomes = list(pool.map(
                    lambda run: driver.execute(self.s, fixture, run, scope=target,
                                               project_id=self.session.project_id,
                                               should_stop=lambda: self._stop_reason(state)),
                    runs))
            for reservation, outcome in zip(reservations, outcomes):
                accountant.reconcile(reservation, outcome.tool_calls)
            settled = True
            return outcomes
        finally:
            if not settled:  # a refused reservation or a raising run: leave nothing open
                for reservation in reservations:
                    accountant.release(reservation)

    def _denied(self, state, tasks, task_id, decision):
        """A policy refusal stops the campaign. The run left no world mutation."""
        tasks = board.fail_dependents(board.finish(tasks, task_id, "failed"))
        outcome = ("world_not_runnable" if decision.code == "world_not_runnable"
                   else "policy_denied")
        return self._stop(state, "failed", outcome, tasks=tasks,
                          error=decision.error_info().model_dump(mode="json"))

    def _stopped_early(self, state, tasks, task_id, *outcomes):
        """A run stopped mid-way by cancellation or the deadline: its evidence is discarded."""
        if not any(outcome.stopped for outcome in outcomes):
            return None
        tasks = board.finish(tasks, task_id, "failed")
        return self._guard({**state, "tasks": tasks}) or self._stop(
            state, "failed", "run_stopped", tasks=tasks,
            error=self._error("internal", "Run stopped early"))

    # -- nodes ------------------------------------------------------------------------

    def _plan(self, state):
        if (stop := self._guard(state)) is not None:
            return stop
        if state.get("repair", {}) and state["repair"].get("verified"):
            return self._stop(state, "completed", "repair_verified")
        if state["iteration"] >= state["max_iterations"]:
            found = any(f["verdict"] == "failed" for f in state["findings"])
            return self._stop(state, "completed",
                              "failure_found_repair_pending" if found else "no_failure_found")
        iteration = state["iteration"] + 1
        prefix = f"{state['campaign_id']}-i{iteration}"
        cid = state["campaign_id"]
        names = {k: f"{prefix}-{k}" for k in ("propose", "validate", "execute", "verify",
                                              "remember")}
        tasks = [*state["tasks"],
                 board.new_task(cid, names["propose"], "propose_experiment", "explorer",
                                max_attempts=2,
                                exit_condition="A valid experiment_proposal/1 is returned"),
                 board.new_task(cid, names["validate"], "validate_proposal", "supervisor",
                                depends_on=[names["propose"]],
                                exit_condition="Proposal fits policy, ceilings and budget"),
                 board.new_task(cid, names["execute"], "execute_run", "target",
                                depends_on=[names["validate"]],
                                exit_condition="Baseline and fork runs complete"),
                 board.new_task(cid, names["verify"], "verify_run", "verifier",
                                depends_on=[names["execute"]],
                                exit_condition="Findings derived from ledger evidence"),
                 board.new_task(cid, names["remember"], "record_memory", "supervisor",
                                depends_on=[names["verify"]],
                                exit_condition="Verified findings stored in memory")]
        accountant = self._accountant(state)
        baselines = {**state.get("task_baselines", {}),
                     names["propose"]: {r: accountant.committed(r) for r in MODEL_RESOURCES}}
        return {"iteration": iteration, "tasks": tasks, "proposal": None,
                "experiment_id": None, "next": "propose", "task_baselines": baselines,
                "events": self._event(state, "campaign_status_changed",
                                      f"Iteration {iteration} planned")}

    def _propose(self, state):
        if (stop := self._guard(state)) is not None:
            return stop
        task_id = f"{self._prefix(state)}-propose"
        tasks = board.start(state["tasks"], task_id)
        attempt = next(t for t in tasks if t["task_id"] == task_id)["attempt"]
        driver = self._world_driver(state)
        spec = driver.spec_model.model_validate(state["spec"])
        explored = [driver.explored(e["proposal"]) for e in state["experiments"].values()]
        questions = (("Already explored: " + "; ".join(explored) + ". Propose something new.",)
                     if explored else ())
        limits = self._inference_limits(state)  # refuses an unauthorized runtime
        accountant = self._accountant(state, limits)
        baseline = state.get("task_baselines", {}).get(task_id, {})
        # Task usage continues across task retries and crash recovery: everything the
        # ledger committed since the task was planned belongs to this task (it is the
        # only model caller between plan and execute).
        accountant.seed_task_usage(task_id, {r: accountant.committed(r) - baseline.get(r, 0)
                                             for r in MODEL_RESOURCES} if baseline else {})
        remaining_ms = limits.max_wall_clock_ms - self._elapsed_ms(state)
        try:
            outcome = self.explorer.propose(
                scope=delegate(self._supervisor(state), "explorer", set()),
                campaign_id=state["campaign_id"],
                task_id=task_id, request_id=f"{task_id}-a{attempt}", objective=spec.objective,
                success_criteria=tuple(spec.success_criteria), as_of=self._now(),
                questions=questions, accountant=accountant,
                task_limits=ceiling_for("explorer"), remaining_ms=remaining_ms,
                deadline_passed=lambda: self._deadline_passed(state), cancel=self.cancel,
                skill_id=driver.skill_id, world_version=driver.world_version,
                proposal_model=driver.proposal_model)
        except ContextError as error:
            tasks = board.fail_dependents(board.finish(tasks, task_id, "failed"))
            return self._stop(state, "failed", "context_error", tasks=tasks,
                              error=self._error("validation_failed", str(error)))
        if outcome.exhausted:
            return self._exhausted(state, outcome.exhausted,
                                   board.finish(tasks, task_id, "failed"))
        if outcome.proposal is not None:
            tasks = board.finish(tasks, task_id, "succeeded")
            return {"proposal": outcome.proposal, "tasks": tasks, "next": "validate",
                    "events": self._event(state, "task_status_changed",
                                          f"Explorer proposed: {outcome.proposal['hypothesis']}",
                                          task_id=task_id)}
        error = outcome.reply.get("error") or self._error(
            "provider_invalid_output", f"Explorer reply status {outcome.reply['status']}")
        if error["code"] == "cancelled" or self.cancel.is_set():
            return self._stop(state, "cancelled", "cancelled_by_request", error=error)
        task = next(t for t in tasks if t["task_id"] == task_id)
        if task["attempt"] < task["max_attempts"] and (
                error["retryable"] or error["code"] == "provider_invalid_output"):
            return {"tasks": board.retry(tasks, task_id), "next": "propose",
                    "errors": [*state["errors"], error],
                    "events": self._event(state, "task_status_changed",
                                          f"Explorer attempt {attempt} failed: {error['code']}",
                                          task_id=task_id)}
        tasks = board.fail_dependents(board.finish(tasks, task_id, "failed"))
        return self._stop(state, "failed", "explorer_failed", tasks=tasks, error=error)

    def _validate(self, state):
        task_id = f"{self._prefix(state)}-validate"
        tasks = board.start(state["tasks"], task_id)
        driver = self._world_driver(state)
        proposal = driver.proposal_model.model_validate(state["proposal"])
        cap = proposal.scenario.max_tool_calls
        limits, accountant = self._limits(state), self._accountant(state)
        used_tools = accountant.usage(wall_clock_ms=0).tool_calls
        canonical = json.dumps(proposal.model_dump(mode="json"), sort_keys=True)
        experiment_id = "exp-" + hashlib.sha256(
            f"{state['campaign_id']}\x1f{canonical}".encode()).hexdigest()[:16]
        problems = driver.problems(proposal.model_dump(mode="json"))
        if cap > ceiling_for("target").max_tool_calls:
            problems.append(f"max_tool_calls {cap} exceeds the target role ceiling")
        if used_tools + 2 * cap > limits.max_tool_calls:
            problems.append("two runs at this cap exceed the remaining tool budget")
        if experiment_id in state["experiments"]:
            problems.append("this experiment was already run")
        if problems:
            tasks = board.fail_dependents(board.finish(tasks, task_id, "failed"))
            error = self._error("validation_failed", "; ".join(problems))
            return {"tasks": tasks, "proposal": None, "next": "plan",
                    "errors": [*state["errors"], error],
                    "events": self._event(state, "task_status_changed",
                                          "Proposal rejected: " + "; ".join(problems),
                                          task_id=task_id)}
        return {"tasks": board.finish(tasks, task_id, "succeeded"), "experiment_id": experiment_id,
                "next": "execute",
                "events": self._event(state, "task_status_changed", "Proposal validated",
                                      task_id=task_id)}

    def _execute(self, state):
        if (stop := self._guard(state)) is not None:
            return stop
        task_id = f"{self._prefix(state)}-execute"
        tasks = board.start(state["tasks"], task_id)
        experiment_id = state["experiment_id"]
        try:
            driver = self._world_driver(state)  # the world may have stopped being runnable
            runs = driver.experiment_runs(state["proposal"])
            baseline, fork = self._run_pair(state, task_id, runs)
        except BudgetExhausted as error:
            return self._exhausted(state, error.resource, board.finish(tasks, task_id, "failed"))
        except PolicyDenied as denied:
            return self._denied(state, tasks, task_id, denied.decision)
        if (stop := self._stopped_early(state, tasks, task_id, baseline, fork)) is not None:
            return stop
        denial = baseline.denial or fork.denial
        if denial is not None:
            return self._denied(state, tasks, task_id, denial)
        driver.validate_run(baseline.run)
        driver.validate_run(fork.run)
        diff = driver.diff(self._fixture(state), baseline.run, fork.run)
        run_ref = {"kind": "run", "id": f"{experiment_id}-fork", "sha256": None}
        baseline_ref = self._store_json(f"{experiment_id}-baseline", baseline.run, run_ref)
        fork_ref = self._store_json(f"{experiment_id}-fork", fork.run, run_ref)
        record = {"experiment_id": experiment_id, "proposal": state["proposal"],
                  "baseline": baseline_ref, "fork": fork_ref, "diff": diff,
                  "iteration": state["iteration"]}
        if runs[0].mode is not None:  # the same mode for both, recorded with the evidence
            record["execution"] = runs[0].mode.as_json()
        tasks = board.finish(tasks, task_id, "succeeded", output_ref=fork_ref)
        return {"tasks": tasks, "experiments": {**state["experiments"], experiment_id: record},
                "next": "verify",
                "events": self._event(state, "run_completed", driver.run_summary(diff),
                                      task_id=task_id, run_id=f"{experiment_id}-fork",
                                      refs=[baseline_ref, fork_ref])}

    def _verify(self, state):
        task_id = f"{self._prefix(state)}-verify"
        tasks = board.start(state["tasks"], task_id)
        experiment = state["experiments"][state["experiment_id"]]
        fork = self._load_json(experiment["fork"])
        findings = self._world_driver(state).findings(
            self._fixture(state), f"{state['experiment_id']}-fork", fork, self._now(),
            experiment.get("execution"))
        failed = [f["invariant"] for f in findings if f["verdict"] == "failed"]
        refs = [{"kind": "finding", "id": f["finding_id"], "sha256": None} for f in findings]
        return {"tasks": board.finish(tasks, task_id, "succeeded"),
                "findings": [*state["findings"], *findings], "next": "remember",
                "events": self._event(state, "finding_recorded",
                                      f"Failed invariants: {failed or 'none'}",
                                      task_id=task_id, refs=refs)}

    def _capture(self, state, findings, experiment_ref):
        driver, scope = self._world_driver(state), self._supervisor(state)
        decision = authorize_capability(scope, "write_memory",
                                        project_id=self.session.project_id)
        if not decision.allowed:
            raise PolicyDenied(decision)
        ids = []
        for finding in findings:
            observation = MemoryObservation(
                kind="finding", content=driver.finding_text(finding),
                observed_at=finding["recorded_at"], world_version=driver.world_version,
                provenance=Provenance(source_kind="verifier", observed_at=finding["recorded_at"],
                                      source_ref=Ref(kind="run", id=finding["run_id"],
                                                     sha256=None), trust="verified"),
                campaign_id=state["campaign_id"],
                evidence=(Ref(kind="finding", id=finding["finding_id"], sha256=None),
                          Ref.model_validate(experiment_ref)))
            ids.append(self.memory.capture(scope, observation).record_id)
        return ids

    def _remember(self, state):
        task_id = f"{self._prefix(state)}-remember"
        tasks = board.start(state["tasks"], task_id)
        experiment = state["experiments"][state["experiment_id"]]
        run_id = f"{state['experiment_id']}-fork"
        mine = [f for f in state["findings"] if f["run_id"] == run_id]
        failed = [f for f in mine if f["verdict"] == "failed"]
        ids = self._capture(state, failed or [f for f in mine if f["evidence_event_ids"]],
                            experiment["fork"])
        tasks = board.finish(tasks, task_id, "succeeded")
        repair_candidate = (failed and state.get("approval") is None
                            and self._world_driver(state).repair_candidate(experiment["proposal"]))
        return {"tasks": tasks, "memory_ids": [*state["memory_ids"], *ids],
                "next": "request_repair" if repair_candidate else "plan"}

    def _repair_action(self, state) -> dict:
        experiment = state["experiments"][state["experiment_id"]]
        action = self._world_driver(state).repair_action(state["experiment_id"],
                                                         experiment["proposal"])
        if state.get("fixture_sha256"):
            action["fixture_sha256"] = state["fixture_sha256"]  # part of the approved digest
        return action

    def _request_repair(self, state):
        """Supervisor requests approval for the checked-in repaired adapter.

        Deterministic: the Analyst model step needs M00 output contracts that are
        not published yet. The id is deterministic so a re-run finds the request.
        """
        action = self._repair_action(state)
        approval_id = f"approval-{state['experiment_id']}"
        cid = state["campaign_id"]
        wait_id, repair_id = f"{cid}-await-approval", f"{cid}-execute-repair"
        tasks = [*state["tasks"],
                 board.new_task(cid, wait_id, "await_approval", "supervisor",
                                exit_condition="A bound human decision is recorded"),
                 board.new_task(cid, repair_id, "execute_run", "target", depends_on=[wait_id],
                                exit_condition="Repaired run verified under the same fault")]
        tasks = board.finish(board.start(tasks, wait_id), wait_id, "awaiting_approval")
        try:
            request = self.s.approvals.request(
                scope=self._supervisor(state), approval_id=approval_id, campaign_id=cid,
                task_id=wait_id,
                action_kind="apply_repair", action=action,
                payload_ref_id=state["experiments"][state["experiment_id"]]["fork"]["id"],
                summary=self._world_driver(state).repair_summary,
                material_risks=["Changes which adapter later runs use"], now=self.s.clock(),
                ttl=self.approval_ttl)
        except ApprovalDenied as error:
            if error.code != "conflict":
                raise
            request = self.s.approval_store.get(self.session.project_id, approval_id)
            if request.action_digest != action_digest(action):
                raise ApprovalDenied("digest_mismatch", "existing approval is for another action")
        approval = {"approval_id": approval_id, "action_digest": request.action_digest,
                    "action": action, "expires_at": request.expires_at}
        return {"tasks": tasks, "approval": approval, "status": "awaiting_approval",
                "next": "await_approval",
                "events": self._event(state, "approval_requested",
                                      f"Approval {approval_id} requested for apply_repair",
                                      task_id=wait_id,
                                      refs=[{"kind": "approval", "id": approval_id,
                                             "sha256": request.action_digest}])}

    def _await_approval(self, state):
        approval = state["approval"]
        interrupt({"approval_id": approval["approval_id"],
                   "action_digest": approval["action_digest"]})  # resume value is ignored
        stored = self.s.approval_store.get(self.session.project_id, approval["approval_id"])
        wait_id = f"{state['campaign_id']}-await-approval"
        if stored.status == "pending":
            if self.s.clock() >= parse_timestamp(stored.expires_at):
                try:
                    self.s.approval_store.mark_expired(self.session.project_id,
                                                       approval["approval_id"])
                except Conflict:
                    pass
                tasks = board.fail_dependents(board.finish(state["tasks"], wait_id, "failed"))
                return self._stop(state, "failed", "approval_expired", tasks=tasks,
                                  error=self._error("approval_invalid", "Approval expired"))
            return {"next": "await_approval"}  # still pending: pause again
        if stored.status == "approved":
            return {"next": "consume", "events": self._event(
                state, "approval_decided", f"Approval {stored.approval_id} approved")}
        tasks = board.fail_dependents(board.finish(state["tasks"], wait_id, "failed"))
        if stored.status == "rejected":
            return self._stop({**state, "status": "running"}, "completed", "repair_rejected",
                              tasks=tasks, kind="approval_decided")
        return self._stop(state, "failed", f"approval_{stored.status}", tasks=tasks,
                          error=self._error("approval_invalid", f"Approval is {stored.status}"))

    def _consume(self, state):
        """Single-use consumption, checkpointed before the repaired run executes."""
        approval = state["approval"]
        wait_id = f"{state['campaign_id']}-await-approval"
        if (problem := self._settings_problem(state)) is not None:  # before single use
            tasks = board.fail_dependents(board.finish(state["tasks"], wait_id, "failed"))
            return self._stop(state, "failed", "settings_changed", tasks=tasks,
                              error=self._error("approval_invalid", problem))
        try:
            self.s.approvals.consume(project_id=self.session.project_id,
                                     approval_id=approval["approval_id"],
                                     action=approval["action"], now=self.s.clock())
        except ApprovalDenied as error:
            tasks = board.fail_dependents(board.finish(state["tasks"], wait_id, "failed"))
            return self._stop(state, "failed", f"approval_{error.code}", tasks=tasks,
                              error=self._error("approval_invalid", str(error)))
        tasks = board.finish(board.finish(state["tasks"], wait_id, "running"), wait_id,
                             "succeeded")
        return {"tasks": tasks, "status": "running", "next": "execute_repair",
                "repair": {"authorized": True, "verified": False}}

    def _execute_repair(self, state):
        if (stop := self._guard(state)) is not None:
            return stop
        repair_id = f"{state['campaign_id']}-execute-repair"
        tasks = board.start(state["tasks"], repair_id)
        action = state["approval"]["action"]
        try:
            driver = self._world_driver(state)  # the world may have stopped being runnable
            request = driver.repair_request(action)
            (outcome,) = self._run_pair(state, repair_id, [request])
        except BudgetExhausted as error:
            return self._exhausted(state, error.resource,
                                   board.finish(tasks, repair_id, "failed"))
        except PolicyDenied as denied:
            return self._denied(state, tasks, repair_id, denied.decision)
        if (stop := self._stopped_early(state, tasks, repair_id, outcome)) is not None:
            return stop
        if outcome.denial is not None:
            return self._denied(state, tasks, repair_id, outcome.denial)
        driver.validate_run(outcome.run)
        run_id = f"{state['experiment_id']}-repaired"
        ref = self._store_json(run_id, outcome.run, {"kind": "run", "id": run_id, "sha256": None})
        execution = request.mode.as_json() if request.mode is not None else None
        fixture = self._fixture(state)
        findings = driver.findings(fixture, run_id, outcome.run, self._now(), execution)
        assessment = driver.assess_repair(fixture, outcome.run, findings, execution)
        verified = assessment["verified"]
        ids = self._capture(state, [f for f in findings if f["evidence_event_ids"]], ref)
        tasks = board.finish(tasks, repair_id, "succeeded" if verified else "failed",
                             output_ref=ref)
        update = self._stop({**state, "findings": [*state["findings"], *findings]},
                            *self._repair_verdict(assessment), tasks=tasks)
        update.update({"findings": [*state["findings"], *findings],
                       "memory_ids": [*state["memory_ids"], *ids],
                       "repair": {"authorized": True, "run": ref, **assessment,
                                  **driver.repair_result(outcome.run, action, verified)}})
        return update

    def _repair_verdict(self, assessment):
        """Passing checks on a run that never got there verify nothing."""
        if assessment["run_status"] != "completed":
            return "budget_exhausted", "repair_incomplete"
        if assessment["verified"]:
            return "completed", "repair_verified"
        return "failed", "repair_did_not_hold" if not assessment["safety_held"] \
            else "repair_incomplete"
