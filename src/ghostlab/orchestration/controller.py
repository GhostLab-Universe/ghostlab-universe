"""M05 campaign controller: an explicit LangGraph loop (``ghostlab-orchestration/1``).

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
"""

import hashlib
import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ghostlab.budgets import BudgetAccountant, BudgetExhausted, utc_timestamp
from ghostlab.context import ContextCompiler, ContextError
from ghostlab.contracts import (BudgetLimits, CampaignRecord, CampaignSpec, ExperimentEvent,
                                ExperimentProposal, Principal, Provenance, RealityDiff, Ref,
                                RunResult)
from ghostlab.contracts.primitives import parse_timestamp
from ghostlab.core import Fixture, Scenario, reality_diff
from ghostlab.memory import MemoryPipeline
from ghostlab.memory.pipeline import Observation as MemoryObservation
from ghostlab.policy import (ApprovalDenied, ApprovalService, PolicyDenied, PrincipalBinding,
                             action_digest, authorize_capability, ceiling_for,
                             check_within_ceiling, delegate, root_scope)
from ghostlab.policy.authorization import deny
from ghostlab.providers import ProviderAdapter, Runtime
from ghostlab.skills import SkillRegistry
from ghostlab.storage.interfaces import ApprovalStore, Conflict, MemoryStore

from . import tasks as board
from .engine import derive_findings, execute_run
from .inference import ExplorerClient, LiveInferenceAuthorization, require_inference_allowed

ORCHESTRATION_VERSION = "ghostlab-orchestration/1"
TARGET_TOOLS = {"get_order", "issue_refund", "get_refund_status"}
ROUTES = ("plan", "propose", "validate", "execute", "verify", "remember", "request_repair",
          "await_approval", "consume", "execute_repair")


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
        within = check_within_ceiling("supervisor", spec.budget)
        if not within.allowed:
            raise PolicyDenied(within)
        model = self.s.adapter_factory(self.s.runtime).config.model
        limits = require_inference_allowed(self.s.runtime, model, spec.budget, self.s.live)
        now = utc_timestamp(self.s.clock())
        state: CampaignState = {
            "campaign_id": campaign_id, "project_id": self.session.project_id,
            "principal_id": self.session.principal.principal_id,
            "trace_id": hashlib.sha256(campaign_id.encode()).hexdigest()[:32],
            "spec": spec.model_dump(mode="json"), "limits": limits.model_dump(mode="json"),
            "status": "running", "outcome": None, "iteration": 0,
            "max_iterations": self.max_iterations, "started_at": now, "tasks": [],
            "proposal": None, "experiment_id": None, "experiments": {}, "findings": [],
            "memory_ids": [], "approval": None, "repair": None, "events": [], "errors": [],
            "next": "plan"}
        state["events"] = self._event(state, "campaign_created", "Campaign created")
        return self._invoke(state, campaign_id)

    def resume(self, campaign_id: str) -> dict:
        """Re-check a paused campaign. The resume payload grants nothing."""
        self._check_owner(campaign_id)
        if not self.graph.get_state(self._config(campaign_id)).next:
            return self.state(campaign_id)  # finished: nothing to resume
        return self._invoke(Command(resume="check"), campaign_id)

    def recover(self, campaign_id: str) -> dict:
        """Continue after a crash from the last checkpoint (graph resume, not replay).

        Completed nodes are not re-run; the interrupted node runs again.
        """
        self._check_owner(campaign_id)
        return self._invoke(None, campaign_id)

    def state(self, campaign_id: str) -> dict:
        snapshot = self.graph.get_state(self._config(campaign_id))
        return {**snapshot.values, "paused": bool(snapshot.next)}

    def record(self, campaign_id: str) -> CampaignRecord:
        state = self.state(campaign_id)
        self._check_owner(campaign_id)
        status = "awaiting_approval" if state["paused"] else state["status"]
        return CampaignRecord.model_validate({
            "schema_version": 1, "campaign_id": campaign_id, "project_id": state["project_id"],
            "created_by": self.session.principal.model_dump(), "spec": state["spec"],
            "status": status, "usage": self._accountant(state).usage(
                wall_clock_ms=self._elapsed_ms(state)).model_dump(mode="json"),
            "trace_id": state["trace_id"], "created_at": state["started_at"],
            "updated_at": utc_timestamp(self.s.clock())})

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
        return self.state(campaign_id)

    @staticmethod
    def _config(campaign_id):
        return {"configurable": {"thread_id": campaign_id}}

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

    def _accountant(self, state) -> BudgetAccountant:
        return BudgetAccountant(self.s.ledger, state["campaign_id"], self._limits(state),
                                clock=self.s.clock)

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

    def _store_json(self, artifact_id, value, kind_ref) -> dict:
        data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        provenance = Provenance(source_kind="controller", source_ref=kind_ref,
                                observed_at=self._now(), trust="trusted")
        try:
            ref = self.s.artifacts.put(data, artifact_id=artifact_id, media_type="application/json",
                                       provenance=provenance, created_at=self._now())
        except Conflict:
            ref = self.s.artifacts.ref(artifact_id)  # re-run after a crash: same content id
            if ref.sha256 != hashlib.sha256(data).hexdigest():
                raise
        return {"kind": "artifact", "id": ref.artifact_id, "sha256": ref.sha256}

    def _load_json(self, ref: dict):
        return json.loads(self.s.artifacts.read(self.s.artifacts.ref(ref["id"])))

    def _run_pair(self, state, task_id, runs):
        """Execute (scenario, adapter) runs in parallel within campaign limits."""
        target = delegate(self.root, "target", TARGET_TOOLS)
        accountant = self._accountant(state)
        task_cap = ceiling_for("target").max_tool_calls * len(runs)
        reservations = [accountant.reserve(task_id, "tool_calls", scenario.max_tool_calls,
                                           task_limit=task_cap) for scenario, _ in runs]
        workers = max(1, min(self._limits(state).max_parallel_runs, len(runs)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(
                lambda item: self.s.engine(self.s.fixture, item[0], item[1], scope=target,
                                           project_id=self.session.project_id,
                                           binding=self.s.binding), runs))
        for reservation, outcome in zip(reservations, outcomes):
            accountant.reconcile(reservation, outcome.tool_calls)
        return outcomes

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
        return {"iteration": iteration, "tasks": tasks, "proposal": None,
                "experiment_id": None, "next": "propose",
                "events": self._event(state, "campaign_status_changed",
                                      f"Iteration {iteration} planned")}

    def _propose(self, state):
        if (stop := self._guard(state)) is not None:
            return stop
        task_id = f"{self._prefix(state)}-propose"
        tasks = board.start(state["tasks"], task_id)
        attempt = next(t for t in tasks if t["task_id"] == task_id)["attempt"]
        spec = CampaignSpec.model_validate(state["spec"])
        explored = [f"adapter={e['proposal']['adapter']} "
                    f"timeout_after_commit={e['proposal']['scenario']['timeout_after_commit']}"
                    for e in state["experiments"].values()]
        questions = (("Already explored: " + "; ".join(explored) + ". Propose something new.",)
                     if explored else ())
        try:
            outcome = self.explorer.propose(
                scope=delegate(self.root, "explorer", set()), campaign_id=state["campaign_id"],
                task_id=task_id, request_id=f"{task_id}-a{attempt}", objective=spec.objective,
                success_criteria=tuple(spec.success_criteria), as_of=self._now(),
                questions=questions, accountant=self._accountant(state),
                task_limits=ceiling_for("explorer"), cancel=self.cancel)
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
        proposal = ExperimentProposal.model_validate(state["proposal"])
        cap = proposal.scenario.max_tool_calls
        limits, accountant = self._limits(state), self._accountant(state)
        used_tools = accountant.usage(wall_clock_ms=0).tool_calls
        canonical = json.dumps(proposal.model_dump(mode="json"), sort_keys=True)
        experiment_id = "exp-" + hashlib.sha256(
            f"{state['campaign_id']}\x1f{canonical}".encode()).hexdigest()[:16]
        problems = []
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
        proposal = ExperimentProposal.model_validate(state["proposal"])
        experiment_id = state["experiment_id"]
        cap = proposal.scenario.max_tool_calls
        runs = [(Scenario(timeout_after_commit=False, max_tool_calls=cap), proposal.adapter),
                (Scenario(**proposal.scenario.model_dump()), proposal.adapter)]
        try:
            baseline, fork = self._run_pair(state, task_id, runs)
        except BudgetExhausted as error:
            return self._exhausted(state, error.resource, board.finish(tasks, task_id, "failed"))
        except PolicyDenied as denied:
            tasks = board.fail_dependents(board.finish(tasks, task_id, "failed"))
            return self._stop(state, "failed", "policy_denied", tasks=tasks,
                              error=denied.decision.error_info().model_dump(mode="json"))
        denial = baseline.denial or fork.denial
        if denial is not None:
            tasks = board.fail_dependents(board.finish(tasks, task_id, "failed"))
            return self._stop(state, "failed", "policy_denied", tasks=tasks,
                              error=denial.error_info().model_dump(mode="json"))
        RunResult.model_validate(baseline.run)
        RunResult.model_validate(fork.run)
        diff = RealityDiff.model_validate(reality_diff(baseline.run, fork.run))
        run_ref = {"kind": "run", "id": f"{experiment_id}-fork", "sha256": None}
        baseline_ref = self._store_json(f"{experiment_id}-baseline", baseline.run, run_ref)
        fork_ref = self._store_json(f"{experiment_id}-fork", fork.run, run_ref)
        record = {"experiment_id": experiment_id, "proposal": state["proposal"],
                  "baseline": baseline_ref, "fork": fork_ref,
                  "diff": diff.model_dump(mode="json"), "iteration": state["iteration"]}
        tasks = board.finish(tasks, task_id, "succeeded", output_ref=fork_ref)
        first = diff.first_divergence.event_id if diff.first_divergence else None
        return {"tasks": tasks, "experiments": {**state["experiments"], experiment_id: record},
                "next": "verify",
                "events": self._event(state, "run_completed",
                                      f"Baseline {diff.baseline_refunded_minor} vs fork "
                                      f"{diff.faulted_refunded_minor} minor units; first "
                                      f"divergence {first}", task_id=task_id,
                                      run_id=f"{experiment_id}-fork",
                                      refs=[baseline_ref, fork_ref])}

    def _verify(self, state):
        task_id = f"{self._prefix(state)}-verify"
        tasks = board.start(state["tasks"], task_id)
        experiment = state["experiments"][state["experiment_id"]]
        fork = self._load_json(experiment["fork"])
        findings = derive_findings(f"{state['experiment_id']}-fork", fork,
                                   recorded_at=self._now())
        failed = [f["invariant"] for f in findings if f["verdict"] == "failed"]
        refs = [{"kind": "finding", "id": f["finding_id"], "sha256": None} for f in findings]
        return {"tasks": board.finish(tasks, task_id, "succeeded"),
                "findings": [*state["findings"], *findings], "next": "remember",
                "events": self._event(state, "finding_recorded",
                                      f"Failed invariants: {failed or 'none'}",
                                      task_id=task_id, refs=refs)}

    def _capture(self, state, findings, experiment_ref):
        decision = authorize_capability(self.root, "write_memory",
                                        project_id=self.session.project_id)
        if not decision.allowed:
            raise PolicyDenied(decision)
        ids = []
        for finding in findings:
            content = (f"{finding['invariant']} {finding['verdict']} in run {finding['run_id']}"
                       + (f": observed {finding['observed_minor']} vs limit "
                          f"{finding['limit_minor']} minor units"
                          if finding["observed_minor"] is not None else ""))
            observation = MemoryObservation(
                kind="finding", content=content, observed_at=finding["recorded_at"],
                provenance=Provenance(source_kind="verifier", observed_at=finding["recorded_at"],
                                      source_ref=Ref(kind="run", id=finding["run_id"],
                                                     sha256=None), trust="verified"),
                campaign_id=state["campaign_id"],
                evidence=(Ref(kind="finding", id=finding["finding_id"], sha256=None),
                          Ref.model_validate(experiment_ref)))
            ids.append(self.memory.capture(self.root, observation).record_id)
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
                            and experiment["proposal"]["adapter"] == "vulnerable")
        return {"tasks": tasks, "memory_ids": [*state["memory_ids"], *ids],
                "next": "request_repair" if repair_candidate else "plan"}

    def _repair_action(self, state) -> dict:
        experiment = state["experiments"][state["experiment_id"]]
        return {"kind": "apply_repair", "experiment_id": state["experiment_id"],
                "adapter": "repaired", "world_version": "refund-world/1",
                "scenario": experiment["proposal"]["scenario"]}

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
                scope=self.root, approval_id=approval_id, campaign_id=cid, task_id=wait_id,
                action_kind="apply_repair", action=action,
                payload_ref_id=state["experiments"][state["experiment_id"]]["fork"]["id"],
                summary="Run the checked-in repaired adapter (status check before retry) "
                        "under the same post-commit timeout",
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
        scenario = Scenario(**action["scenario"])
        try:
            (outcome,) = self._run_pair(state, repair_id, [(scenario, action["adapter"])])
        except BudgetExhausted as error:
            return self._exhausted(state, error.resource,
                                   board.finish(tasks, repair_id, "failed"))
        if outcome.denial is not None:
            return self._stop(state, "failed", "policy_denied",
                              tasks=board.finish(tasks, repair_id, "failed"),
                              error=outcome.denial.error_info().model_dump(mode="json"))
        RunResult.model_validate(outcome.run)
        run_id = f"{state['experiment_id']}-repaired"
        ref = self._store_json(run_id, outcome.run, {"kind": "run", "id": run_id, "sha256": None})
        findings = derive_findings(run_id, outcome.run, recorded_at=self._now())
        held = all(f["verdict"] == "passed" for f in findings)
        ids = self._capture(state, [f for f in findings if f["evidence_event_ids"]], ref)
        tasks = board.finish(tasks, repair_id, "succeeded" if held else "failed", output_ref=ref)
        update = self._stop({**state, "findings": [*state["findings"], *findings]},
                            "completed" if held else "failed",
                            "repair_verified" if held else "repair_did_not_hold", tasks=tasks)
        update.update({"findings": [*state["findings"], *findings],
                       "memory_ids": [*state["memory_ids"], *ids],
                       "repair": {"authorized": True, "verified": held, "run": ref,
                                  "refunded_minor": outcome.run["final_state"][
                                      "total_refunded_minor"]}})
        return update
