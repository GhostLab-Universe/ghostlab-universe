"""Control-plane operations: HTTP-free adapters over M05, M06 and M04.

The controller stays authoritative. Status, tasks, events and evidence come from its
checkpoint; ``CampaignController`` checks ownership on each call; ``ApprovalService``
decides; artifacts go through ``FileArtifactStore.for_project``. This module keeps no
database of its own, so a restart loses nothing but which threads were running.

Request identity needs no table either. A campaign id is derived from the session and
the ``Idempotency-Key``, and a decision id likewise, so the checkpoint and the approval
record themselves answer "did this already happen?".
"""

import dataclasses
import hashlib
import json
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from pathlib import Path

import httpx
from fastapi.responses import Response
from pydantic import ValidationError

from ghostlab.budgets import utc_timestamp
from ghostlab.capsule import MAX_CAPSULE_BYTES
from ghostlab.contracts import (ApprovalDecision, ApprovalRequest, ApprovalSubmission, CapsuleV1,
                                ErrorInfo, ExperimentEvent, Provenance, Ref, TaskSpec)
from ghostlab.contracts.primitives import parse_timestamp
from ghostlab.contracts.world import (CapsuleV2, WorldCampaignRecord, WorldCampaignSpec,
                                      WorldExperimentProposal, WorldInvariantFinding,
                                      WorldRealityDiff, WorldRunResult)
from ghostlab.contracts.world_packs import PACKS
from ghostlab.contracts.world_refund import (REFUND, capsule_v2_to_v1, diff_to_world,
                                             finding_to_world, proposal_to_world, run_to_world)
from ghostlab.orchestration import (CampaignController, CampaignSettingsChanged, Services,
                                    TrustedSession)
from ghostlab.orchestration.controller import CampaignExists, ledger_key
from ghostlab.policy import (ApprovalDenied, ApproverAuthority, PolicyDenied, action_digest,
                             authorize_capability, authorize_world, root_scope)
from ghostlab.policy.authorization import deny
from ghostlab.security.redaction import Redactor
from ghostlab.storage.interfaces import Conflict, NotFound

from .errors import ApiError, describe, not_found
from .jobs import Job, JobRegistry, ServerStopping
from .models import (ApprovalSummary, ApprovalView, CampaignView, CapsuleImport, CapsuleInfo,
                     Coverage, DecisionResult, ErrorResponse, EventPage, ExecutionRecord,
                     ExperimentSummary, ExperimentView, GuardDenial, JobStatus, RealityDiffView,
                     ReplayResult, RepairView, RunName, RunView, SessionInfo)
from .scripted import (DEFAULT_SCENARIO, SCRIPTED_ENDPOINT, ScriptedProposals, available,
                       scripted_runtime)
from .sessions import ApiSession

CANCEL_WAIT_S = 10.0  # how long a cancel waits to learn whether it ended a stopped campaign
MAX_LISTED_ERRORS = 256
MAX_TEXT = 2_000  # the contracts' Text limit; a redaction marker can lengthen a short match
VERDICTS = {"completed", "failed", "cancelled", "budget_exhausted"}  # only M05's _stop sets these


@dataclass(frozen=True)
class ApiConfig:
    """Server-side wiring. Credentials stay inside ``services``; no response carries them."""

    services: Services
    sessions: object
    approvers: ApproverAuthority
    graph_db: Path
    app_db: Path | None = None
    bind_host: str = "127.0.0.1"
    redactor: Redactor = dataclasses.field(default_factory=Redactor.from_env)
    max_active_campaigns: int = 4
    max_iterations: int = 2
    approval_ttl: timedelta = timedelta(hours=1)
    environ: dict | None = None
    scripted: bool = False  # the explicit opt-in: see ghostlab.api.scripted


def request_digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def _capsule_bytes(envelope: dict) -> bytes:
    return (json.dumps(envelope, indent=2) + "\n").encode()


def _keys(value) -> Iterator[str]:
    """Every dict key, at any depth. ``Redactor.value`` reads keys but never scrubs them."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            yield from item
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


class PinningController(CampaignController):
    """``start`` with one step between its gates and its first checkpoint.

    A refused start stores nothing, so it leaves no pin; a failed pin leaves no campaign;
    and nothing can read a campaign before its pin. M05 has no hook here (M07.md, requests).
    """

    def __init__(self, *args, pin: Callable[[], None], **options):
        super().__init__(*args, **options)
        self._pin = pin

    def _invoke(self, payload, campaign_id):
        self._pin()
        return super()._invoke(payload, campaign_id)


class ControlPlane:
    def __init__(self, config: ApiConfig, saver):
        self.config, self.services, self.saver = config, config.services, saver
        self.redactor = config.redactor
        self.jobs = JobRegistry(limit=config.max_active_campaigns,
                                on_failure=self._record_failure)
        self._failures: dict[str, ErrorInfo] = {}
        self._origins: dict[str, dict] = {}

    # -- plumbing ---------------------------------------------------------------------

    def _controller(self, session: TrustedSession, *, services: Services | None = None,
                    cancel=None, pin: Callable[[], None] | None = None) -> CampaignController:
        options = {"saver": self.saver, "max_iterations": self.config.max_iterations,
                   "approval_ttl": self.config.approval_ttl, "cancel": cancel}
        if pin is not None:
            return PinningController(session, services or self.services, pin=pin, **options)
        return CampaignController(session, services or self.services, **options)

    def _record_failure(self, key: str, error: BaseException, cancelling: bool):
        _, body, _ = describe(error if isinstance(error, Exception) else RuntimeError(),
                              self.redactor)
        info = body.error.model_dump(mode="json")
        if cancelling:  # the flag ends with the worker, and this must not read as cancelled
            info["message"] = f"Cancellation did not complete: {info['message']}"[:MAX_TEXT]
        self._failures[key] = ErrorInfo.model_validate(info)

    def _launch(self, session: TrustedSession, campaign_id: str,
                call: Callable[[CampaignController], object], *, cancel: bool = False,
                follow: bool = False, scenario: str | None = None) -> tuple[Job, bool]:
        """``scenario`` is given only by a start, which also pins the campaign's origin."""
        key = ledger_key(session.project_id, campaign_id)

        def work(job: Job):
            self._failures.pop(key, None)
            hook = self.services.on_node
            replies = None
            if self.config.scripted:
                pinned = (self._origin(session, campaign_id) or {}).get("scripted_scenario")
                # A stop serves no proposal, so it needs no scenario. Anything else would guess.
                if scenario is None and pinned is None and not job.cancel.is_set():
                    raise CampaignSettingsChanged("this campaign has no recorded scenario")
                replies = ScriptedProposals(scenario or pinned or DEFAULT_SCENARIO)

            def on_node(name, state):
                job.entered = True  # the checkpoint exists: this campaign can be read
                job.settled.set()
                if self.jobs.stopping.is_set():
                    raise ServerStopping()
                if replies is not None:
                    replies.observe(state)
                hook(name, state)

            services = dataclasses.replace(
                self.services, on_node=on_node,
                runtime=scripted_runtime(replies) if replies else self.services.runtime)
            pin = None if scenario is None else partial(self._pin_origin, session, campaign_id,
                                                        scenario)
            call(self._controller(session, services=services, cancel=job.cancel, pin=pin))

        return self.jobs.submit(key, work, cancel=cancel, follow=follow)

    def _pin_origin(self, session, campaign_id: str, scenario: str) -> None:
        """What produced this campaign, fixed at start, so no later server can relabel it."""
        origin = {"inference": self._inference(),
                  "scripted_scenario": scenario if self.config.scripted else None}
        stamp = utc_timestamp(self.services.clock())
        provenance = Provenance(source_kind="controller", source_ref=None, observed_at=stamp,
                                trust="trusted")
        view = self.services.artifacts.for_project(self._scope(session))
        try:
            view.put(json.dumps(origin, sort_keys=True).encode(), name=f"{campaign_id}-origin",
                     media_type="application/json", provenance=provenance, created_at=stamp)
        except Conflict:
            # An earlier start stored this and no checkpoint. A pin never changes, so this
            # one can only be that same start.
            if self._origin(session, campaign_id) != origin:
                raise self._key_reused() from None

    def _origin(self, session, campaign_id: str) -> dict | None:
        key = ledger_key(session.project_id, campaign_id)
        if key not in self._origins:
            view = self.services.artifacts.for_project(self._scope(session))
            try:
                data = view.read(view.ref(view.artifact_id(f"{campaign_id}-origin")))
            except NotFound:
                return None
            self._origins[key] = json.loads(data)
        return self._origins[key]

    def _text(self, value: str) -> str:
        return self.redactor.text(value)[:MAX_TEXT]

    def _refuse_secrets(self, *texts: str | None) -> None:
        """Client text is M07's to keep clean: M05 redacts only what models write.

        Refused rather than rewritten, so a stored spec is what the client sent and a
        retry still compares equal. The response never repeats the text.
        """
        if any(text and self.redactor.text(text) != text for text in texts):
            raise self._secret_refused()

    @staticmethod
    def _secret_refused() -> ApiError:
        return ApiError(422, "validation_failed", "This text looks like it contains a "
                        "credential. Remove it and retry; nothing was stored.",
                        reason="secret_shaped_text")

    def _scope(self, session: ApiSession):
        return root_scope(session.project_id, session.principal)

    def _evidence(self, session: ApiSession):
        """This project's artifacts, after the session's evidence capability."""
        scope = self._scope(session)
        if not (allowed := authorize_capability(scope, "read_evidence",
                                                project_id=session.project_id)).allowed:
            raise PolicyDenied(allowed)
        return self.services.artifacts.for_project(scope)

    # -- session ----------------------------------------------------------------------

    def _inference(self) -> str:
        if self.config.scripted:
            return "scripted"
        transport = self.services.runtime.transport
        return ("fixture" if isinstance(transport, httpx.MockTransport)
                else "live" if self.services.live is not None else "disabled")

    def _campaign_inference(self, session, state: dict) -> str:
        """As pinned at start. Never the reading server's mode, which proves nothing."""
        origin = self._origin(session, state["campaign_id"])
        if origin is not None:
            return origin["inference"]
        endpoint = (state.get("provider") or {}).get("endpoint")
        return "scripted" if endpoint == SCRIPTED_ENDPOINT else "unrecorded"

    def _can_approve(self, principal, project_id: str) -> bool:
        """What ``ApprovalService.decide`` will accept, so the session can't overstate it."""
        if principal.kind == "service" or (principal.kind == "local_dev"
                                           and not self.services.approvals._local_dev_mode):
            return False
        return self.config.approvers.may_approve(principal, project_id)

    def session_info(self, session: ApiSession) -> SessionInfo:
        worlds = sorted(world for world in PACKS
                        if world in self.services.worlds and authorize_world(world).allowed)
        return SessionInfo(schema_version=1, principal=session.principal,
                           project_id=session.project_id, auth_mode=session.auth_mode,
                           production_authenticated=False,
                           can_approve=self._can_approve(session.principal, session.project_id),
                           inference=self._inference(), runnable_worlds=worlds)

    # -- campaigns --------------------------------------------------------------------

    def _require_scenario(self, world_version: str, scenario: str) -> None:
        """Only a scripted server has scenarios to name, and not every world has each."""
        if scenario != DEFAULT_SCENARIO and not (self.config.scripted
                                                 and available(world_version, scenario)):
            raise ApiError(422, "validation_failed", "This server or world has no such scripted "
                           "scenario", reason="scripted_scenario_unavailable")

    def create_campaign(self, session: ApiSession, spec: WorldCampaignSpec, idempotency_key: str,
                        scenario: str = DEFAULT_SCENARIO) -> tuple[CampaignView, bool]:
        self._refuse_secrets(spec.objective, *spec.success_criteria)
        self._require_scenario(spec.world_version, scenario)
        trusted = session.trusted
        campaign_id = "c-" + request_digest(session.project_id, session.principal.principal_id,
                                            idempotency_key)
        replayed = self._existing(trusted, campaign_id) is not None
        if not replayed:
            job, started = self._launch(trusted, campaign_id,
                                        lambda controller: controller.start(campaign_id, spec),
                                        scenario=scenario)
            try:
                self.jobs.wait_for_first_step(job)  # the controller's gates run and raise here
            except CampaignExists:
                started = False
            except ValidationError as error:  # the only one before a checkpoint exists
                raise ApiError(422, "validation_failed", "This project's identifiers are too "
                               "long to derive a campaign's ids; nothing was stored",
                               reason="identifier_too_long") from error
            replayed = not started
        state = self._controller(trusted).state(campaign_id)
        pinned = (self._origin(trusted, campaign_id) or {}).get("scripted_scenario")
        if (state["spec"] != spec.model_dump(mode="json")
                or (pinned or DEFAULT_SCENARIO) != scenario):
            raise self._key_reused()
        return self.campaign_view(session, campaign_id), replayed

    @staticmethod
    def _key_reused() -> ApiError:
        return ApiError(409, "conflict", "This Idempotency-Key was used for a different spec",
                        reason="idempotency_key_reused")

    def _existing(self, trusted: TrustedSession, campaign_id: str) -> dict | None:
        try:
            return self._controller(trusted).state(campaign_id)
        except PolicyDenied:
            return None

    def _job_status(self, project_id: str, key: str, job: Job | None, state: dict) -> JobStatus:
        if job is not None:
            return JobStatus(state="active", cancel_requested=job.cancel.is_set(), error=None)
        error = self._failures.get(key)
        unfinished = state["paused"]
        stalled = unfinished and (state["status"] != "awaiting_approval" or error is not None
                                  or self._decided(project_id, state))
        return JobStatus(state="stalled" if stalled else "idle", cancel_requested=False,
                         error=error if unfinished else None)

    def _decided(self, project_id: str, state: dict) -> bool:
        """A decided approval with no worker: only a resume can move this campaign on."""
        approval = state.get("approval")
        return approval is not None and self.services.approval_store.get(
            project_id, approval["approval_id"]).status != "pending"

    def _errors(self, state: dict) -> list[ErrorInfo]:
        return [ErrorInfo.model_validate({**error, "message": self._text(error["message"])})
                for error in state["errors"][-MAX_LISTED_ERRORS:]]

    def _proposal_summary(self, experiment: dict, state: dict) -> ExperimentSummary:
        proposal = experiment["proposal"]
        return ExperimentSummary(experiment_id=experiment["experiment_id"],
                                 iteration=experiment["iteration"],
                                 hypothesis=self._text(proposal["hypothesis"]),
                                 adapter_id=proposal.get("adapter_id") or proposal["adapter"],
                                 coverage=self._coverage(experiment),
                                 runs=self._runs(state, experiment["experiment_id"]))

    @staticmethod
    def _coverage(experiment: dict) -> Coverage | None:
        coverage = experiment.get("coverage")
        return Coverage.model_validate({"faults_triggered": None, **coverage}) if coverage \
            else None

    @staticmethod
    def _repaired_experiment(state: dict) -> str | None:
        repair = state.get("repair") or {}
        return state["approval"]["action"]["experiment_id"] if repair.get("run") else None

    def _runs(self, state: dict, experiment_id: str) -> list[RunName]:
        repaired = self._repaired_experiment(state) == experiment_id
        return ["baseline", "fork", *(["repaired"] if repaired else [])]

    @staticmethod
    def _repair(state: dict) -> RepairView | None:
        repair = state.get("repair")
        if not repair:
            return None
        execution = repair.get("execution")
        denials = repair.get("guard_denials")
        return RepairView(authorized=repair["authorized"], verified=repair["verified"],
                          run=Ref.model_validate(repair["run"]) if repair.get("run") else None,
                          run_status=repair.get("run_status"),
                          safety_held=repair.get("safety_held"),
                          task_complete=repair.get("task_complete"),
                          task_outcome=repair.get("task_outcome"), adapter=repair.get("adapter"),
                          execution=ExecutionRecord(**execution) if execution else None,
                          guard_denials=(None if denials is None
                                         else [GuardDenial(**denial) for denial in denials]),
                          held_by=repair.get("held_by"), lost_faults=repair.get("lost_faults"),
                          refunded_minor=repair.get("refunded_minor"))

    def campaign_view(self, session: ApiSession, campaign_id: str) -> CampaignView:
        controller = self._controller(session.trusted)
        key = ledger_key(session.project_id, campaign_id)
        job = self.jobs.get(key)  # before the state: a worker ending now mustn't read as a stall
        state = controller.state(campaign_id)  # the owner check: otherwise reads as not found
        # One view, one read: record() reads again, and a worker may have moved on since.
        record = WorldCampaignRecord.model_validate(
            {**controller.record(campaign_id).model_dump(mode="json"), "status": state["status"]})
        approval = state.get("approval")
        return CampaignView(
            record=record, inference=self._campaign_inference(session, state), outcome=state["outcome"],
            fixture_sha256=state.get("fixture_sha256"),
            job=self._job_status(session.project_id, key, job, state),
            tasks=[TaskSpec.model_validate(task) for task in state["tasks"]],
            approval=(ApprovalSummary(approval_id=approval["approval_id"],
                                      action_digest=approval["action_digest"],
                                      expires_at=approval["expires_at"]) if approval else None),
            experiments=[self._proposal_summary(experiment, state)
                         for experiment in state["experiments"].values()],
            repair=self._repair(state), errors=self._errors(state),
            event_count=len(state["events"]))

    def events(self, session: ApiSession, campaign_id: str, after: int, limit: int) -> EventPage:
        state = self._controller(session.trusted).state(campaign_id)
        pending = [event for event in state["events"] if event["sequence"] > after]
        page = [ExperimentEvent.model_validate({**event, "summary": self._text(event["summary"])})
                for event in pending[:limit]]
        return EventPage(campaign_id=campaign_id, inference=self._campaign_inference(session, state),
                         events=page,
                         next_after=page[-1].sequence if page else after,
                         has_more=len(pending) > limit)

    @staticmethod
    def _ended(state: dict) -> bool:
        """Nothing is left to run. ``paused`` alone can't say so.

        While a step's writes wait for their checkpoint, LangGraph reports no next node, so
        a live campaign briefly reads as unpaused. Only the status marks a real end.
        """
        return state["status"] in VERDICTS and not state["paused"]

    @staticmethod
    def _proceed(campaign_id: str) -> Callable[[CampaignController], object]:
        """Run the graph on: past an approval pause, or from a crash's last checkpoint."""
        def call(controller: CampaignController):
            state = controller.state(campaign_id)
            if state["paused"]:
                paused = state["status"] == "awaiting_approval"
                return (controller.resume if paused else controller.recover)(campaign_id)
            return state

        return call

    def cancel(self, session: ApiSession, campaign_id: str) -> CampaignView:
        """Stop at the next guard, whatever is or isn't running.

        A worker already running sees the flag, and its result reads in the view. A paused
        or stalled campaign needs a worker to reach its guard, so this waits to see that
        it did: a stalled node can fail every time, and M05 has no guard before it runs
        (M07.md, M05 requests). A cancel that didn't stop anything says so. The follow-up
        covers a worker on its way to a pause. M05 checks the guard before consuming, so
        an unused approval stays unspent.
        """
        trusted = session.trusted
        controller = self._controller(trusted)
        state = controller.state(campaign_id)
        if state["status"] in VERDICTS:  # even with its memory step left: nothing to stop
            if state["status"] != "cancelled":
                raise ApiError(409, "conflict", f"Campaign is already {state['status']}",
                               reason="campaign_finished")
            return self.campaign_view(session, campaign_id)
        job, started = self._launch(trusted, campaign_id, self._proceed(campaign_id),
                                    cancel=True, follow=True)
        job.cancel.set()
        if started:
            self.jobs.wait_for_first_step(job)
            if job.finished.wait(CANCEL_WAIT_S) and controller.state(campaign_id)["paused"]:
                raise ApiError(409, "conflict", "The campaign did not stop: its interrupted "
                               "step could not finish, so its status is unchanged",
                               reason="cancel_incomplete")
        return self.campaign_view(session, campaign_id)

    def resume(self, session: ApiSession, campaign_id: str) -> CampaignView:
        trusted = session.trusted
        state = self._controller(trusted).state(campaign_id)
        if self._ended(state):
            raise ApiError(409, "conflict", f"Campaign is already {state['status']}",
                           reason="campaign_finished")
        # Following a running worker matters only at a pause; see JobRegistry.submit.
        job, started = self._launch(trusted, campaign_id, self._proceed(campaign_id),
                                    follow=state["status"] == "awaiting_approval")
        if started:
            self.jobs.wait_for_first_step(job)
        return self.campaign_view(session, campaign_id)

    # -- evidence ---------------------------------------------------------------------

    def _experiment(self, state: dict, experiment_id: str) -> dict:
        experiment = state["experiments"].get(experiment_id)
        if experiment is None:
            raise not_found()
        return experiment

    def _require_run(self, state: dict, experiment_id: str, run: str) -> dict:
        experiment = self._experiment(state, experiment_id)
        if run not in self._runs(state, experiment_id):  # no repair, or another experiment's
            raise not_found()
        return experiment

    @staticmethod
    def _world_finding(state: dict, finding: dict) -> WorldInvariantFinding:
        if state["spec"]["world_version"] == REFUND:
            return finding_to_world(finding)
        return WorldInvariantFinding.model_validate(finding)

    def _findings(self, state: dict, run_ids: set[str]) -> list[WorldInvariantFinding]:
        return [self._world_finding(state, finding) for finding in state["findings"]
                if finding["run_id"] in run_ids]

    @staticmethod
    def _world_diff(state: dict, experiment: dict) -> WorldRealityDiff:
        if state["spec"]["world_version"] == REFUND:
            return diff_to_world(experiment["diff"])
        return WorldRealityDiff.model_validate(experiment["diff"])

    def _world_proposal(self, state: dict, experiment: dict) -> WorldExperimentProposal:
        proposal = experiment["proposal"]
        if state["spec"]["world_version"] == REFUND:
            proposal = proposal_to_world(proposal).model_dump(mode="json")
        clean = self.redactor.value(proposal)
        return WorldExperimentProposal.model_validate({
            **clean, "hypothesis": clean["hypothesis"][:MAX_TEXT],
            "expected_signal": clean["expected_signal"][:MAX_TEXT]})

    @staticmethod
    def _execution(value: dict | None) -> ExecutionRecord | None:
        return ExecutionRecord(**value) if value else None

    def experiment(self, session: ApiSession, campaign_id: str,
                   experiment_id: str) -> ExperimentView:
        state = self._controller(session.trusted).state(campaign_id)
        experiment = self._experiment(state, experiment_id)
        runs = self._runs(state, experiment_id)
        return ExperimentView(
            experiment_id=experiment_id, inference=self._campaign_inference(session, state),
            iteration=experiment["iteration"],
            proposal=self._world_proposal(state, experiment),
            execution=self._execution(experiment.get("execution")),
            coverage=self._coverage(experiment), diff=self._world_diff(state, experiment),
            findings=self._findings(state, {f"{experiment_id}-{run}" for run in runs}), runs=runs)

    def diff(self, session: ApiSession, campaign_id: str,
             experiment_id: str) -> RealityDiffView:
        state = self._controller(session.trusted).state(campaign_id)
        experiment = self._experiment(state, experiment_id)
        return RealityDiffView(experiment_id=experiment_id,
                               inference=self._campaign_inference(session, state),
                               diff=self._world_diff(state, experiment),
                               coverage=self._coverage(experiment))

    def _stored_run(self, session: ApiSession, ref: dict) -> dict:
        view = self._evidence(session)  # never the unscoped store
        return json.loads(view.read(view.ref(ref["id"])))

    @staticmethod
    def _run_ref(state: dict, experiment: dict, run: RunName) -> dict:
        return (state.get("repair") or {})["run"] if run == "repaired" else experiment[run]

    def run(self, session: ApiSession, campaign_id: str, experiment_id: str,
            run: RunName) -> RunView:
        state = self._controller(session.trusted).state(campaign_id)
        experiment = self._require_run(state, experiment_id, run)
        ref = self._run_ref(state, experiment, run)
        execution = ((state.get("repair") or {}).get("execution") if run == "repaired"
                     else experiment.get("execution"))
        data = self._stored_run(session, ref)
        result = (run_to_world(data) if state["spec"]["world_version"] == REFUND
                  else WorldRunResult.model_validate(data))
        run_id = f"{experiment_id}-{run}"
        return RunView(inference=self._campaign_inference(session, state), run_id=run_id,
                       experiment_id=experiment_id, run=run,
                       artifact=Ref.model_validate(ref), execution=self._execution(execution),
                       result=result, findings=self._findings(state, {run_id}))

    def artifact_response(self, session: ApiSession, artifact_id: str) -> Response:
        view = self._evidence(session)
        ref = view.ref(artifact_id)
        return Response(view.read(ref), media_type=ref.media_type, headers={
            "Content-Disposition": f'attachment; filename="{artifact_id}.json"',
            "ETag": f'"{ref.sha256}"'})

    # -- capsules ---------------------------------------------------------------------

    @staticmethod
    def _capsule_schema(world_version: str) -> int:
        return 1 if world_version == REFUND else 2

    def export_capsule(self, session: ApiSession, campaign_id: str, experiment_id: str,
                       run: RunName) -> CapsuleInfo:
        controller = self._controller(session.trusted)
        state = controller.state(campaign_id)
        self._require_run(state, experiment_id, run)
        ref = controller.export_capsule(campaign_id, run=run, experiment_id=experiment_id)
        world_version = state["spec"]["world_version"]
        return CapsuleInfo(artifact=Ref.model_validate(ref), world_version=world_version,
                           capsule_schema_version=self._capsule_schema(world_version),
                           inference=self._campaign_inference(session, state))

    @staticmethod
    def _replay_result(world_version: str, run_status: str, result: dict,
                       inference: str | None = None) -> ReplayResult:
        metrics = result.get("metrics", {"refunded_minor": result.get("total_refunded_minor", 0)})
        return ReplayResult(world_version=world_version, inference=inference,
                            # Replay checks the status only for a recording it reproduced.
                            run_status=run_status if result["reproduced"] else None,
                            reproduced=result["reproduced"],
                            matches=result["matches"], checks=result["checks"], metrics=metrics,
                            guarded=result.get("guarded"), model_calls=result["model_calls"])

    def replay_campaign_capsule(self, session: ApiSession, campaign_id: str, experiment_id: str,
                                run: RunName) -> ReplayResult:
        controller = self._controller(session.trusted)
        state = controller.state(campaign_id)
        experiment = self._require_run(state, experiment_id, run)
        recorded = self._stored_run(session, self._run_ref(state, experiment, run))
        return self._replay_result(state["spec"]["world_version"], recorded["status"],
                                   self._replaying(lambda: controller.replay_capsule(
                                       campaign_id, run=run, experiment_id=experiment_id)),
                                   self._campaign_inference(session, state))

    @staticmethod
    def _replaying(call: Callable[[], dict]) -> dict:
        try:
            return call()
        except (ValueError, TypeError, KeyError) as error:
            raise ApiError(422, "validation_failed", "This capsule cannot be replayed",
                           reason="capsule_invalid") from error

    def _driver(self, world_version: str):
        """The runnable gate, as for a campaign: a capsule can't name its way past it."""
        if not (gate := authorize_world(world_version)).allowed:
            raise PolicyDenied(gate)
        driver = self.services.worlds.get(world_version)
        if driver is None:
            raise PolicyDenied(deny("world_not_runnable", "no checked-in implementation"))
        return driver

    def _replay_bytes(self, world_version: str, data: bytes) -> ReplayResult:
        driver = self._driver(world_version)

        def replay() -> dict:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "capsule.json"
                path.write_bytes(data)
                return driver.replay(path)

        result = self._replaying(replay)  # replay refuses a status its adapter doesn't produce
        return self._replay_result(world_version, json.loads(data)["payload"]["run"]["status"],
                                   result)

    def import_capsule(self, session: ApiSession, capsule: CapsuleV1 | CapsuleV2) -> CapsuleImport:
        view = self._evidence(session)
        world_version = capsule.payload.world_version
        self._driver(world_version)
        if isinstance(capsule, CapsuleV2) and world_version == REFUND:
            try:
                capsule = capsule_v2_to_v1(capsule)  # refund replay reads v1
            except ValueError as error:
                raise ApiError(422, "validation_failed", "This capsule cannot be replayed",
                               reason="capsule_invalid") from error
        envelope = capsule.model_dump(mode="json")
        if (self.redactor.value(envelope) != envelope  # stored, and served to the whole project
                or any(self.redactor.text(key) != key for key in _keys(envelope))):
            raise self._secret_refused()
        data = _capsule_bytes(envelope)
        if len(data) > MAX_CAPSULE_BYTES:
            raise ApiError(413, "validation_failed", "Capsule too large", reason="too_large")
        replay = self._replay_bytes(world_version, data)  # refused before anything is stored
        name, now = f"imported-{capsule.sha256}", self.services.clock()
        stamp = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        provenance = Provenance(source_kind="human", source_ref=None, observed_at=stamp,
                                trust="untrusted")
        try:
            ref = view.put(data, name=name, media_type="application/json",
                           provenance=provenance, created_at=stamp)
        except Conflict:
            ref = view.ref(view.artifact_id(name))  # the same checksum: the same capsule
        artifact = Ref(kind="artifact", id=ref.artifact_id, sha256=ref.sha256)
        return CapsuleImport(capsule=CapsuleInfo(
            artifact=artifact, world_version=world_version,
            capsule_schema_version=self._capsule_schema(world_version), inference=None),
            replay=replay)

    def replay_capsule(self, session: ApiSession, capsule_id: str) -> ReplayResult:
        view = self._evidence(session)
        try:
            data = view.read(view.ref(capsule_id), max_bytes=MAX_CAPSULE_BYTES)
            world_version = json.loads(data)["payload"]["world_version"]
        except (ValueError, KeyError, TypeError) as error:
            raise ApiError(422, "validation_failed", "This artifact is not a capsule",
                           reason="not_a_capsule") from error
        return self._replay_bytes(world_version, data)

    # -- approvals --------------------------------------------------------------------

    def _approval_state(self, request: ApprovalRequest, project_id: str) -> dict | None:
        owner = TrustedSession(request.scope.principal, project_id)
        try:
            return self._controller(owner).state(request.campaign_id)
        except PolicyDenied:
            return None

    @staticmethod
    def _approval_action(request: ApprovalRequest, state: dict | None) -> dict | None:
        """The action the digest binds, shown only if the digest still proves it."""
        action = ((state or {}).get("approval") or {}).get("action")
        return action if action and action_digest(action) == request.action_digest else None

    def approval(self, session: ApiSession, approval_id: str) -> ApprovalView:
        store, project_id = self.services.approval_store, session.project_id
        request = store.get(project_id, approval_id)  # scoped: another project's is not found
        principal = session.principal
        if (request.scope.principal.principal_id != principal.principal_id
                and not self.config.approvers.may_approve(principal, project_id)):
            raise not_found()
        expired = (request.status in {"pending", "approved"}  # consume refuses either one
                   and self.services.clock() >= parse_timestamp(request.expires_at))
        state = self._approval_state(request, project_id)
        return ApprovalView(request=request,
                            inference=self._campaign_inference(session, state) if state else None,
                            effective_status="expired" if expired else request.status,
                            action=self._approval_action(request, state),
                            payload_artifact_sha256=self._artifact_digest(session,
                                                                          request.payload.id),
                            decision=store.get_decision(project_id, approval_id))

    def _artifact_digest(self, session: ApiSession, artifact_id: str) -> str | None:
        try:
            return self._evidence(session).ref(artifact_id).sha256
        except (PolicyDenied, NotFound):
            return None

    def _resume_for_owner(self, request: ApprovalRequest,
                          project_id: str) -> tuple[bool, ErrorResponse | None]:
        """Continue the campaign as its owner; the resume itself authorizes nothing.

        The controller only runs a repair past a stored, single-use approval. The owner
        comes from the approval, which the controller recorded from its session.
        """
        owner = TrustedSession(request.scope.principal, project_id)
        try:
            state = self._controller(owner).state(request.campaign_id)
            if self._ended(state):
                # Read again: a retry's copy can predate the worker that just used the approval.
                stored = self.services.approval_store.get(project_id, request.approval_id)
                if stored.status == "consumed" or state["outcome"] == "repair_rejected":
                    return True, None  # a retry: this decision is what moved the campaign
                raise ApiError(409, "conflict", f"Campaign is already {state['status']}",
                               reason="campaign_finished")  # stopped before this decision
            job, started = self._launch(owner, request.campaign_id,
                                        self._proceed(request.campaign_id), follow=True)
            if started:
                self.jobs.wait_for_first_step(job)
        except Exception as error:  # the decision is recorded whatever happens to the resume
            return False, describe(error, self.redactor)[1]
        return True, None

    def decide(self, session: ApiSession, approval_id: str, submission: ApprovalSubmission,
               idempotency_key: str) -> tuple[DecisionResult, bool]:
        if submission.approval_id != approval_id:
            raise ApiError(422, "validation_failed", "approval_id differs from the path",
                           reason="invalid_request")
        self._refuse_secrets(submission.reason)
        store, project_id = self.services.approval_store, session.project_id
        decision_id = "d-" + request_digest(project_id, session.principal.principal_id,
                                            approval_id, idempotency_key)
        replayed = False
        try:
            decision = self.services.approvals.decide(
                session_principal=session.principal, project_id=project_id,
                submission=submission, decision_id=decision_id, now=self.services.clock())
        except ApprovalDenied as denied:
            if denied.code == "expired":  # the campaign can now end instead of staying paused
                self._resume_for_owner(store.get(project_id, approval_id), project_id)
                raise
            if denied.code not in {"not_pending", "conflict"}:  # conflict: lost a race
                raise
            decision = store.get_decision(project_id, approval_id)
            if decision is None or decision.decision_id != decision_id:
                # Seen a moment later, the same race is not_pending: clients get one answer.
                raise ApprovalDenied("not_pending", "approval is no longer pending") from denied
            self._require_same_decision(decision, submission)
            replayed = True
        request = store.get(project_id, approval_id)
        resumed, error = self._resume_for_owner(request, project_id)
        return DecisionResult(decision=decision, approval=request, resumed=resumed,
                              resume_error=error), replayed

    @staticmethod
    def _require_same_decision(recorded: ApprovalDecision, submission: ApprovalSubmission):
        if (recorded.decision, recorded.action_digest, recorded.reason) != (
                submission.decision, submission.action_digest, submission.reason):
            raise ApiError(409, "conflict", "This Idempotency-Key was used for another decision",
                           reason="idempotency_key_reused")
