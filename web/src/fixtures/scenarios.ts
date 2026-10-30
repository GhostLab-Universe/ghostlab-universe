/**
 * TEST DATA. The fixture data source serves these scenarios so the interface can be
 * exercised without a backend. Nothing here came from a running service.
 *
 * Evidence is real where it can be: runs, diffs, findings and capsules are published M00
 * examples or recorded output of the checked-in core. Tasks, events and campaign states
 * are hand-authored in the shape M05 produces, and every scenario says so in its origins.
 */
import type {
  ApprovalDecision,
  ApprovalRequest,
  ApprovalView,
  CampaignView,
  CapsuleV1,
  CapsuleV2,
  Coverage,
  ErrorResponse,
  ExecutionRecord,
  ExperimentEvent,
  ReplayResult,
  TaskSpec,
  WorldExperimentProposal,
  WorldInvariantFinding,
  WorldRealityDiff,
  WorldRunResult,
} from "../api/api.generated.ts";
import type { Failure, Origin, RunName } from "../data/source.ts";
import {
  approval_decision__approved,
  approval_request__apply_repair,
  campaign_record__running,
  capsule_v1__refund_failure,
  capsule_v2__journey_fixture,
  error_info__committed_timeout,
  experiment_event__finding_recorded,
  world_campaign_record__group_journey,
  world_experiment_proposal__group_journey_revocation,
  world_experiment_proposal__refund_timeout,
  world_reality_diff__journey_fixture,
  world_reality_diff__refund_demo,
  world_run_result__journey_fixture,
  world_run_result__refund_faulted,
} from "../../../schemas/typescript/examples.check.ts";
import { recorded } from "./recorded.generated.ts";
import type { RecordedAction, RecordedRepair, RecordedTruncation } from "./recordedTypes.ts";

const published = (file: string, note: string | null = null): Origin => ({
  kind: "published_example",
  source: `examples/contracts/${file}`,
  note,
});
const core = (name: string, note: string | null = null): Origin => ({
  kind: "recorded_core_output",
  source: `recorded.generated.ts: ${name}`,
  note,
});
const authored = (note: string): Origin => ({ kind: "hand_authored", source: "fixtures/scenarios.ts", note });

export interface RunFixture {
  run_id: string;
  result: WorldRunResult;
  findings: WorldInvariantFinding[];
  origins: readonly Origin[];
}

export interface ExperimentFixture {
  experiment_id: string;
  proposal: WorldExperimentProposal;
  execution: ExecutionRecord | null;
  diff: WorldRealityDiff;
  findings: WorldInvariantFinding[];
  coverage: Coverage;
  runs: Partial<Record<RunName, RunFixture>>;
  replay: ReplayResult;
  capsule: CapsuleV1 | CapsuleV2;
  capsuleSchemaVersion: 1 | 2;
  origins: readonly Origin[];
  capsuleOrigins: readonly Origin[];
}

export interface Stage {
  view: CampaignView;
  events: ExperimentEvent[];
  approval: ApprovalView | null;
  experiment: ExperimentFixture | null;
  origins: readonly Origin[];
}

export interface Scenario {
  id: string;
  group: "flow" | "state";
  title: string;
  shows: string;
  stage: Stage;
  /** What an approve, reject or resume moves the fake service to. */
  approved?: Stage;
  rejected?: Stage;
  cancelled?: Stage;
  resumed?: Stage;
  loadFailure?: Failure;
  decisionFailure?: Failure;
  resumeError?: ErrorResponse;
  delayMs?: number;
}

const serviceFailure = (
  status: number,
  code: ErrorResponse["error"]["code"],
  message: string,
  reason: string | null,
): Failure => ({
  kind: "service",
  status,
  reason,
  error: { code, message, ref: null, retryable: false, side_effect: "none" },
});

/* Tasks follow Controller._plan and _request_repair; statuses are authored per stage. */
type StatusList = readonly TaskSpec["status"][];
const PLAN = [
  ["propose", "propose_experiment", "explorer", "A valid experiment_proposal/1 is returned"],
  ["validate", "validate_proposal", "supervisor", "Proposal fits policy, ceilings and budget"],
  ["execute", "execute_run", "target", "Baseline and fork runs complete"],
  ["verify", "verify_run", "verifier", "Findings derived from ledger evidence"],
  ["remember", "record_memory", "supervisor", "Verified findings stored in memory"],
] as const;

function tasks(campaignId: string, statuses: StatusList, repair: StatusList = []): TaskSpec[] {
  const base = (id: string, kind: TaskSpec["kind"], role: TaskSpec["role"], exit: string, deps: string[], status: TaskSpec["status"]): TaskSpec => ({
    schema_version: 1,
    task_id: id,
    campaign_id: campaignId,
    kind,
    role,
    depends_on: deps,
    status,
    attempt: status === "pending" ? 0 : 1,
    max_attempts: kind === "propose_experiment" ? 2 : 1,
    exit_condition: exit,
    output_ref: null,
  });
  const name = (step: string) => `${campaignId}-i1-${step}`;
  const plan = PLAN.flatMap(([step, kind, role, exit], index) => {
    const status = statuses[index];
    if (status === undefined) return [];
    const previous = PLAN[index - 1];
    return [base(name(step), kind, role, exit, previous ? [name(previous[0])] : [], status)];
  });
  const [waiting, repairing] = repair;
  if (waiting === undefined || repairing === undefined) return plan;
  const wait = `${campaignId}-await-approval`;
  return [
    ...plan,
    base(wait, "await_approval", "supervisor", "A bound human decision is recorded", [], waiting),
    base(`${campaignId}-execute-repair`, "execute_run", "target", "Repaired run verified under the same fault", [wait], repairing),
  ];
}

const DONE = ["succeeded", "succeeded", "succeeded", "succeeded", "succeeded"] as const;
const COMPLETE: Coverage = { baseline: "completed", fork: "completed", complete: true };

function event(
  campaignId: string,
  sequence: number,
  kind: ExperimentEvent["kind"],
  summary: string,
  extra: Partial<ExperimentEvent> = {},
): ExperimentEvent {
  return {
    schema_version: 1,
    campaign_id: campaignId,
    sequence,
    kind,
    summary,
    task_id: null,
    run_id: null,
    occurred_at: `2026-10-29T10:00:${String(sequence).padStart(2, "0")}Z`,
    trace_id: experiment_event__finding_recorded.trace_id,
    refs: [],
    ...extra,
  };
}

function campaign(
  record: CampaignView["record"],
  campaignId: string,
  overrides: Partial<Omit<CampaignView, "record">> & { status?: CampaignView["record"]["status"]; updatedAt?: string },
): CampaignView {
  const { status, updatedAt, ...rest } = overrides;
  const bound = record.spec.world_version === "refund-world/1" ? recorded.refund_repair_action : recorded.journey_repair_action;
  return {
    record: {
      ...record,
      campaign_id: campaignId,
      status: status ?? record.status,
      updated_at: updatedAt ?? record.updated_at,
    },
    outcome: null,
    job: { state: "idle", cancel_requested: false, error: null },
    tasks: [],
    approval: null,
    experiments: [],
    repair: null,
    errors: [],
    event_count: 0,
    fixture_sha256: bound.action.fixture_sha256 as string,
    ...rest,
  };
}

function approvalRequest(campaignId: string, bound: RecordedAction, summary: string, status: ApprovalRequest["status"]): ApprovalRequest {
  return {
    ...approval_request__apply_repair,
    approval_id: `approval-${campaignId}`,
    campaign_id: campaignId,
    task_id: `${campaignId}-await-approval`,
    action_digest: bound.action_digest,
    status,
    summary,
  };
}

function decision(request: ApprovalRequest, choice: ApprovalDecision["decision"], reason: string | null): ApprovalDecision {
  return {
    ...approval_decision__approved,
    approval_id: request.approval_id,
    action_digest: request.action_digest,
    decision: choice,
    decision_id: `decision-${request.approval_id}`,
    reason,
  };
}

/* World-specific inputs to the two flows. */
interface World {
  record: CampaignView["record"];
  recordFile: string;
  experiment: (repaired: RecordedRepair | null) => ExperimentFixture;
  action: RecordedAction;
  repairSummary: string;
  repair: RecordedRepair;
}

const refundRuns = {
  baseline: {
    run_id: "run-baseline-1",
    result: recorded.refund_baseline_run,
    findings: [],
    origins: [core("refund_baseline_run", "Published refund demo, mapped with M00's run_to_world")],
  },
  fork: {
    run_id: "run-faulted-1",
    result: world_run_result__refund_faulted,
    findings: recorded.refund_fork_findings,
    origins: [published("core/world_run_result.refund_faulted.json"), core("refund_fork_findings")],
  },
} satisfies Record<string, RunFixture>;

const journeyRuns = {
  baseline: {
    run_id: "journey-run-1-baseline",
    result: recorded.journey_baseline_run,
    findings: [],
    origins: [core("journey_baseline_run")],
  },
  fork: {
    run_id: "journey-run-1",
    result: world_run_result__journey_fixture,
    findings: recorded.journey_fork_findings,
    origins: [published("core/world_run_result.journey_fixture.json"), core("journey_fork_findings")],
  },
} satisfies Record<string, RunFixture>;

const repairedRun = (run_id: string, repair: RecordedRepair, name: string): RunFixture => ({
  run_id,
  result: repair.run,
  findings: repair.findings,
  origins: [core(name)],
});

const REFUND: World = {
  record: campaign_record__running,
  recordFile: "campaign_record.running.json",
  action: recorded.refund_repair_action,
  repairSummary: "Run the checked-in repaired adapter (status check before retry) under the same post-commit timeout",
  repair: recorded.refund_repair,
  experiment: (repaired) => ({
    experiment_id: "experiment-1",
    proposal: world_experiment_proposal__refund_timeout,
    execution: null,
    diff: world_reality_diff__refund_demo,
    findings: recorded.refund_fork_findings,
    coverage: COMPLETE,
    runs: {
      ...refundRuns,
      ...(repaired ? { repaired: repairedRun("run-repaired-1", repaired, "refund_repair") } : {}),
    },
    replay: recorded.refund_replay,
    capsule: capsule_v1__refund_failure,
    capsuleSchemaVersion: 1,
    origins: [
      published("core/world_reality_diff.refund_demo.json"),
      published("world_experiment_proposal.refund_timeout.json"),
      core("refund_fork_findings", "Verifier output for the published faulted run"),
    ],
    capsuleOrigins: [
      published("core/capsule_v1.refund_failure.json"),
      core("refund_replay", "Real replay of that capsule; world_version and metrics follow M07's ReplayResult"),
    ],
  }),
};

const JOURNEY: World = {
  record: world_campaign_record__group_journey,
  recordFile: "world_campaign_record.group_journey.json",
  action: recorded.journey_repair_action,
  repairSummary: "Run the checked-in version-checked adapter on the guarded boundary under the same faults",
  repair: recorded.journey_repair_held_by_adapter,
  experiment: (repaired) => ({
    experiment_id: "experiment-1",
    proposal: world_experiment_proposal__group_journey_revocation,
    execution: { mode: "lab", guarded: false },
    diff: world_reality_diff__journey_fixture,
    findings: recorded.journey_fork_findings,
    coverage: COMPLETE,
    runs: {
      ...journeyRuns,
      ...(repaired ? { repaired: repairedRun("journey-repair-1", repaired, "journey repair") } : {}),
    },
    replay: recorded.journey_replay,
    capsule: capsule_v2__journey_fixture,
    capsuleSchemaVersion: 2,
    origins: [
      published("core/world_reality_diff.journey_fixture.json"),
      published("world_experiment_proposal.group_journey_revocation.json"),
      core("journey_fork_findings", "Verifier output for the published faulted run"),
    ],
    capsuleOrigins: [published("core/capsule_v2.journey_fixture.json"), core("journey_replay")],
  }),
};

function truncatedWorld(truncation: RecordedTruncation, name: string): World {
  return {
    ...JOURNEY,
    action: recorded.journey_truncated_action,
    experiment: () => ({
      ...JOURNEY.experiment(null),
      proposal: {
        ...world_experiment_proposal__group_journey_revocation,
        adapter_id: truncation.fork.adapter_id,
        scenario: truncation.scenario,
      },
      diff: truncation.diff,
      findings: truncation.findings,
      coverage: truncation.coverage,
      replay: truncation.replay,
      capsule: truncation.capsule,
      capsuleOrigins: [core(`${name}.capsule`), core(`${name}.replay`)],
      runs: {
        baseline: { run_id: "journey-run-1-baseline", result: truncation.baseline, findings: [], origins: [core(`${name}.baseline`)] },
        fork: { run_id: "journey-run-1", result: truncation.fork, findings: truncation.findings, origins: [core(`${name}.fork`)] },
      },
      origins: [core(name, "Real truncated runs; coverage built as M05's Controller._execute does")],
    }),
  };
}

const TRUNCATED_CLEAN = truncatedWorld(recorded.journey_truncated_clean, "journey_truncated_clean");
const TRUNCATED_VIOLATION = truncatedWorld(recorded.journey_truncated_violation, "journey_truncated_violation");

const AUTHORED_STATE = authored("Task list, event list and this campaign state are hand-authored in M05's shape");

function originsFor(world: World, note: string): readonly Origin[] {
  return [published(world.recordFile, note), AUTHORED_STATE];
}

function summaryOf(experiment: ExperimentFixture): CampaignView["experiments"] {
  return [
    {
      experiment_id: experiment.experiment_id,
      iteration: 1,
      hypothesis: experiment.proposal.hypothesis,
      adapter_id: experiment.proposal.adapter_id,
      runs: Object.keys(experiment.runs) as CampaignView["experiments"][number]["runs"],
      coverage: experiment.coverage,
    },
  ];
}

function runEvents(campaignId: string, world: World): ExperimentEvent[] {
  const fork = world === REFUND ? "run-faulted-1" : "journey-run-1";
  const finding = world === REFUND ? "run-faulted-1-total_refund_within_payment" : "journey-run-1-delivered_messages_permitted";
  const task = (step: string) => `${campaignId}-i1-${step}`;
  return [
    event(campaignId, 1, "campaign_created", "Campaign created"),
    event(campaignId, 2, "campaign_status_changed", "Iteration 1 started"),
    event(campaignId, 3, "task_status_changed", "Proposal accepted", { task_id: task("validate") }),
    event(campaignId, 4, "run_started", "Baseline and fork started", { task_id: task("execute") }),
    event(campaignId, 5, "run_completed", "Baseline and fork completed", { task_id: task("execute"), run_id: fork }),
    event(campaignId, 6, "finding_recorded", `Verifier recorded ${finding}`, {
      task_id: task("verify"),
      run_id: fork,
      refs: [{ id: finding, kind: "finding", sha256: null }],
    }),
  ];
}

function awaitingStage(campaignId: string, world: World): { stage: Stage; request: ApprovalRequest } {
  const experiment = world.experiment(null);
  const request = approvalRequest(campaignId, world.action, world.repairSummary, "pending");
  const approvalId = request.approval_id;
  const events = [
    ...runEvents(campaignId, world),
    event(campaignId, 7, "approval_requested", `Approval ${approvalId} requested for apply_repair`, {
      task_id: `${campaignId}-await-approval`,
      refs: [{ id: approvalId, kind: "approval", sha256: request.action_digest }],
    }),
  ];
  return {
    request,
    stage: {
      view: campaign(world.record, campaignId, {
        status: "awaiting_approval",
        tasks: tasks(campaignId, DONE, ["awaiting_approval", "pending"]),
        approval: { approval_id: approvalId, action_digest: request.action_digest, expires_at: request.expires_at },
        experiments: summaryOf(experiment),
        event_count: events.length,
      }),
      events,
      approval: { request, effective_status: "pending", action: world.action.action, decision: null },
      experiment,
      origins: originsFor(world, "Id and status replaced for this scenario"),
    },
  };
}

function finishedStage(campaignId: string, world: World, request: ApprovalRequest, repair: RecordedRepair, name: string): Stage {
  const experiment = world.experiment(repair);
  const consumed = { ...request, status: "consumed" as const };
  const verified = repair.outcome === "repair_verified";
  const events = [
    ...awaitingStage(campaignId, world).stage.events,
    event(campaignId, 8, "approval_decided", `Approval ${request.approval_id} approved`),
    event(campaignId, 9, "campaign_status_changed", `Campaign ${repair.campaign_status}: ${repair.outcome}`),
  ];
  return {
    view: campaign(world.record, campaignId, {
      status: repair.campaign_status,
      outcome: repair.outcome,
      updatedAt: "2026-10-29T10:10:00Z",
      tasks: tasks(campaignId, DONE, ["succeeded", verified ? "succeeded" : "failed"]),
      approval: { approval_id: request.approval_id, action_digest: request.action_digest, expires_at: request.expires_at },
      experiments: summaryOf(experiment),
      repair: repair.repair,
      event_count: events.length,
    }),
    events,
    approval: { request: consumed, effective_status: "consumed", action: world.action.action, decision: decision(request, "approve", null) },
    experiment,
    origins: [
      ...originsFor(world, "Id and status replaced for this scenario"),
      core(name, "RepairView from M05's driver at the commit named in recorded.generated.ts"),
    ],
  };
}

function rejectedStage(campaignId: string, world: World, request: ApprovalRequest): Stage {
  const experiment = world.experiment(null);
  const events = [
    ...awaitingStage(campaignId, world).stage.events,
    event(campaignId, 8, "approval_decided", "Campaign completed: repair_rejected"),
  ];
  return {
    view: campaign(world.record, campaignId, {
      status: "completed",
      outcome: "repair_rejected",
      updatedAt: "2026-10-29T10:10:00Z",
      tasks: tasks(campaignId, DONE, ["failed", "skipped"]),
      approval: { approval_id: request.approval_id, action_digest: request.action_digest, expires_at: request.expires_at },
      experiments: summaryOf(experiment),
      event_count: events.length,
    }),
    events,
    approval: { request: { ...request, status: "rejected" }, effective_status: "rejected", action: world.action.action, decision: decision(request, "reject", null) },
    experiment,
    origins: originsFor(world, "Id and status replaced for this scenario"),
  };
}

function runningStage(campaignId: string, world: World, status: CampaignView["record"]["status"], extra: Partial<CampaignView>, note: string): Stage {
  const events = runEvents(campaignId, world).slice(0, 5);
  const experiment = world.experiment(null);
  return {
    view: campaign(world.record, campaignId, {
      status,
      tasks: tasks(campaignId, ["succeeded", "succeeded", "running"]),
      experiments: summaryOf(experiment),
      event_count: events.length,
      ...extra,
    }),
    events,
    approval: null,
    experiment,
    origins: originsFor(world, note),
  };
}

function coverageIncomplete(id: string): Scenario {
  const experiment = TRUNCATED_CLEAN.experiment(null);
  const events = [
    ...runEvents(id, TRUNCATED_CLEAN).filter((item) => item.kind !== "finding_recorded"),
    event(id, 6, "budget_exhausted", "Campaign budget_exhausted: coverage_incomplete"),
  ];
  return {
    id,
    group: "state",
    title: "Coverage incomplete",
    shows: "budget_exhausted / coverage_incomplete: a truncated run with no failure proves nothing.",
    stage: {
      view: campaign(TRUNCATED_CLEAN.record, id, {
        status: "budget_exhausted",
        outcome: "coverage_incomplete",
        updatedAt: "2026-10-29T10:10:00Z",
        tasks: tasks(id, DONE),
        experiments: summaryOf(experiment),
        errors: [
          {
            code: "budget_exhausted",
            message: "1 experiment(s) stopped at their tool cap before finishing",
            ref: null,
            retryable: false,
            side_effect: "none",
          },
        ],
        event_count: events.length,
      }),
      events,
      approval: null,
      experiment,
      origins: originsFor(TRUNCATED_CLEAN, "Id and status replaced for this scenario"),
    },
  };
}

/** Failed with the approval approved and unspent, as Controller._consume leaves it. */
function settingsBlocked(id: string): Scenario {
  const { stage, request } = awaitingStage(id, JOURNEY);
  const approved = { ...request, status: "approved" as const };
  const events = [...stage.events, event(id, 8, "campaign_status_changed", "Campaign failed: settings_changed")];
  return {
    id,
    group: "state",
    title: "Blocked: settings changed",
    shows: "failed / settings_changed: the recorded fixture no longer matches. The approval is unspent.",
    stage: {
      ...stage,
      view: campaign(JOURNEY.record, id, {
        status: "failed",
        outcome: "settings_changed",
        updatedAt: "2026-10-29T10:10:00Z",
        tasks: tasks(id, DONE, ["failed", "skipped"]),
        approval: stage.view.approval,
        experiments: stage.view.experiments,
        errors: [
          {
            code: "approval_invalid",
            message: "the world fixture changed since this campaign started",
            ref: null,
            retryable: false,
            side_effect: "none",
          },
        ],
        event_count: events.length,
      }),
      events,
      approval: {
        request: approved,
        effective_status: "approved",
        action: JOURNEY.action.action,
        decision: decision(request, "approve", null),
      },
    },
  };
}

function redacted(id: string): Scenario {
  const stage = runningStage(id, REFUND, "running", {}, "Id and text replaced for this scenario");
  const experiment = stage.experiment!;
  const hypothesis = "Retrying with key [REDACTED] issues a second refund";
  const withText: ExperimentFixture = { ...experiment, proposal: { ...experiment.proposal, hypothesis } };
  const events = [
    ...stage.events,
    event(id, 6, "task_status_changed", `Explorer proposed: ${hypothesis} (secret-shaped text redacted)`),
  ];
  return {
    id,
    group: "state",
    title: "Redacted text",
    shows: "[REDACTED] and (secret-shaped text redacted) appear as plain text.",
    stage: {
      ...stage,
      experiment: withText,
      events,
      view: {
        ...stage.view,
        experiments: summaryOf(withText),
        errors: [
          { code: "provider_permanent", message: "Provider said token=[REDACTED]", ref: null, retryable: false, side_effect: "none" },
        ],
        event_count: events.length,
      },
    },
  };
}

function flow(id: string, world: World, name: string, title: string, shows: string): Scenario {
  const { stage, request } = awaitingStage(id, world);
  return {
    id,
    group: "flow",
    title,
    shows,
    stage,
    approved: finishedStage(id, world, request, world.repair, name),
    rejected: rejectedStage(id, world, request),
  };
}

function finished(id: string, world: World, repair: RecordedRepair, name: string, title: string, shows: string): Scenario {
  const { request } = awaitingStage(id, world);
  return { id, group: "state", title, shows, stage: finishedStage(id, world, request, repair, name) };
}

function awaiting(id: string, world: World, title: string, shows: string, extra: Partial<Scenario>): Scenario {
  return { id, group: "state", title, shows, stage: awaitingStage(id, world).stage, ...extra };
}

function expired(id: string): Scenario {
  const scenario = awaiting(id, REFUND, "Approval expired", "An approval past expiry: no decision can be made.", {});
  const approval = scenario.stage.approval;
  return approval === null
    ? scenario
    : { ...scenario, stage: { ...scenario.stage, approval: { ...approval, effective_status: "expired" } } };
}

function missing(id: string, title: string, shows: string, loadFailure: Failure): Scenario {
  return { ...runningScenario(id), title, shows, loadFailure };
}

function runningScenario(id: string): Scenario {
  return {
    id,
    group: "state",
    title: "Refund campaign running",
    shows: "A running campaign. Use the test controls to drop and restore the connection.",
    stage: runningStage(id, REFUND, "running", { job: { state: "active", cancel_requested: false, error: null } }, "As published, with a scenario id"),
    cancelled: {
      ...runningStage(id, REFUND, "cancelled", { outcome: "cancelled_by_request", tasks: tasks(id, ["succeeded", "succeeded", "cancelled"]) }, "Id and status replaced for this scenario"),
    },
  };
}

function resumeStalled(id: string): Scenario {
  const error = error_info__committed_timeout;
  const view = (state: "stalled" | "active") =>
    runningStage(id, REFUND, "running", { job: { state, cancel_requested: false, error: state === "stalled" ? error : null } }, "Id and job state replaced for this scenario");
  return {
    id,
    group: "state",
    title: "Campaign stalled",
    shows: "The checkpoint is unfinished and no worker holds it. The owner can resume.",
    stage: view("stalled"),
    resumed: view("active"),
  };
}

export const SCENARIOS: readonly Scenario[] = [
  flow("fx-refund", REFUND, "refund_repair", "Refund: timeout after commit", "Full flow: evidence, diff, capsule replay, then approve or reject the repair."),
  flow("fx-journey", JOURNEY, "journey_repair_held_by_adapter", "Group journey: sharing revoked while queued", "Full flow. The first divergence and the violating event are different events."),
  finished("fx-journey-boundary", JOURNEY, recorded.journey_repair_held_by_boundary, "journey_repair_held_by_boundary", "Repair held by the boundary", "A verified repair where the guard denied a delivery at event 18."),
  finished("fx-journey-truncated", JOURNEY, recorded.journey_repair_truncated, "journey_repair_truncated", "Repair incomplete", "budget_exhausted / repair_incomplete: safety held, but the run never finished."),
  finished("fx-journey-idle", JOURNEY, recorded.journey_repair_idle, "journey_repair_idle", "Repair incomplete, nothing attempted", "failed / repair_incomplete: the run finished without booking, so nothing was tested."),
  coverageIncomplete("fx-coverage-incomplete"),
  awaiting("fx-coverage-violation", TRUNCATED_VIOLATION, "Violation before truncation", "Coverage is incomplete, but the violation stands and still goes to approval.", {}),
  settingsBlocked("fx-settings-blocked"),
  redacted("fx-redacted"),
  runningScenario("fx-running"),
  {
    id: "fx-empty",
    group: "state",
    title: "Queued campaign",
    shows: "Nothing has run yet: every view has an empty state.",
    stage: {
      view: campaign(campaign_record__running, "fx-empty", { status: "queued" }),
      events: [],
      approval: null,
      experiment: null,
      origins: originsFor(REFUND, "Id and status replaced for this scenario"),
    },
  },
  {
    id: "fx-cancelled",
    group: "state",
    title: "Campaign cancelled",
    shows: "A cancelled campaign, with its partial evidence.",
    stage: runningStage("fx-cancelled", REFUND, "cancelled", { outcome: "cancelled_by_request", tasks: tasks("fx-cancelled", ["succeeded", "succeeded", "cancelled"]) }, "Id and status replaced for this scenario"),
  },
  {
    id: "fx-failed",
    group: "state",
    title: "Campaign failed",
    shows: "A failed campaign with a recorded error whose side effect is committed.",
    stage: runningStage("fx-failed", REFUND, "failed", { outcome: "run_failed", errors: [error_info__committed_timeout], tasks: tasks("fx-failed", ["succeeded", "succeeded", "failed"]) }, "Id and status replaced for this scenario"),
  },
  resumeStalled("fx-stalled"),
  awaiting("fx-approval-denied", REFUND, "Approval denied by the service", "This session is refused: the approval stays pending.", {
    decisionFailure: serviceFailure(403, "forbidden", "This principal is not an approver for the project", "approver_not_allowed"),
  }),
  awaiting("fx-resume-failed", JOURNEY, "Decision recorded, resume failed", "The decision stands, but the campaign could not continue yet.", {
    resumeError: {
      reason: "settings_changed",
      error: { code: "conflict", message: "the world fixture changed since this campaign started", ref: null, retryable: false, side_effect: "none" },
    },
  }),
  expired("fx-approval-expired"),
  missing("fx-load-denied", "Campaign read denied", "A 403 on the authoritative read.", serviceFailure(403, "forbidden", "Refused by policy", "capability_denied")),
  missing("fx-load-invalid", "Campaign read rejected", "A 422 validation error.", serviceFailure(422, "validation_failed", "campaign_id does not match the identifier pattern", null)),
  missing("fx-load-missing", "Campaign not found", "A 404: absent, or owned by another project.", serviceFailure(404, "not_found", "No such campaign", null)),
  { ...runningScenario("fx-slow"), title: "Slow read", shows: "The loading state, for four seconds.", delayMs: 4000 },
];
