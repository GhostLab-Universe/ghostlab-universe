import type {
  ApprovalSubmission,
  ExtCampaignReport,
  ExtEnvironmentDetail,
  ExtEnvironmentSummary,
  ExtFidelityReport,
  ExtFixture,
  ExtInspection,
  ExtMinimizeResult,
  ExtQualificationReport,
  ExtReplayResult,
  ExtRetestResult,
  CampaignView,
  DecisionResult,
  ErrorInfo,
  EventPage,
  ExperimentView,
} from "../api/api.generated.ts";
import { SCENARIOS, type ExperimentFixture, type Scenario, type Stage } from "../fixtures/scenarios.ts";
import { isTerminal } from "../logic/status.ts";
import type { DataSource, Failure, Origin, Result, RunName } from "./source.ts";

const PAGE_SIZE = 100;
const TEST_DOUBLE: Origin = {
  kind: "hand_authored",
  source: "data/fixtureSource.ts",
  note: "Test double of the service's response",
};

function refused(status: number, code: ErrorInfo["code"], message: string, reason: string | null): Result<never> {
  return {
    ok: false,
    failure: {
      kind: "service",
      status,
      reason,
      error: { code, message, ref: null, retryable: false, side_effect: "none" },
    },
  };
}

const notFound = (what: string) => refused(404, "not_found", `No ${what} in this test scenario`, null);

const EXT_ENVS: ExtEnvironmentSummary[] = [
  { world_version: "refund-world/1", environment_id: "refund-environment", environment_version: "refund-environment/1", source: "fixture", tools: ["get_order"], faults: ["timeout_after_commit"], invariants: ["total_refund_within_payment"], fixtures: ["synthetic-inr-2500"], capabilities: { snapshot_restore: true }, targets: ["vulnerable", "repaired"] },
  { world_version: "reservation-world/1", environment_id: "reservation-environment", environment_version: "reservation-environment/1", source: "fixture", tools: ["check_availability"], faults: ["timeout_after_commit"], invariants: ["capacity_respected"], fixtures: ["half_full"], capabilities: { snapshot_restore: false }, targets: [] },
];

/** Controls for the fixture source. Only fixture mode has them. */
export interface FixtureHarness {
  readonly scenarios: readonly Scenario[];
  isOffline(): boolean;
  setOffline(offline: boolean): void;
}

export interface FixtureSource extends DataSource {
  readonly harness: FixtureHarness;
}

/** A test double of M07. Its rules mirror the contract's documented responses. */
export function createFixtureSource(scenarios: readonly Scenario[] = SCENARIOS): FixtureSource {
  const stages = new Map(scenarios.map((scenario) => [scenario.id, scenario.stage]));
  const exported = new Set<string>();
  const artifacts = new Map<string, unknown>();
  let offline = false;

  const scenarioOf = (id: string) => scenarios.find((scenario) => scenario.id === id);
  const stageOf = (id: string): Stage | undefined => stages.get(id);

  async function respond<T>(id: string | null, make: () => Result<T>): Promise<Result<T>> {
    const delay = id === null ? 0 : (scenarioOf(id)?.delayMs ?? 0);
    if (delay > 0) await new Promise((resolve) => setTimeout(resolve, delay));
    if (offline) return { ok: false, failure: { kind: "transport", message: "Test control: connection dropped" } };
    return make();
  }

  const ok = <T,>(data: T, origins: readonly Origin[]): Result<T> => ({ ok: true, data, origins });

  function loadFailure(id: string): { ok: false; failure: Failure } | null {
    const failure = scenarioOf(id)?.loadFailure;
    return failure === undefined ? null : { ok: false, failure };
  }

  function withExperiment<T>(campaignId: string, experimentId: string, make: (experiment: ExperimentFixture) => Result<T>): Result<T> {
    const experiment = stageOf(campaignId)?.experiment;
    if (experiment === null || experiment === undefined || experiment.experiment_id !== experimentId) {
      return notFound("experiment");
    }
    return make(experiment);
  }

  function move(campaignId: string, next: Stage | undefined): Result<CampaignView> {
    if (next !== undefined) stages.set(campaignId, next);
    const stage = stageOf(campaignId);
    return stage === undefined ? notFound("campaign") : ok(stage.view, stage.origins);
  }

  function decide(submission: ApprovalSubmission): Result<DecisionResult> {
    const scenario = scenarios.find((item) => stageOf(item.id)?.approval?.request.approval_id === submission.approval_id);
    const stage = scenario && stageOf(scenario.id);
    const approval = stage?.approval;
    if (scenario === undefined || approval === undefined || approval === null) return notFound("approval");
    if (scenario.decisionFailure !== undefined) return { ok: false, failure: scenario.decisionFailure };
    if (submission.action_digest !== approval.request.action_digest) {
      return refused(409, "approval_invalid", "The digest does not match the approval request", "digest_mismatch");
    }
    if (approval.effective_status !== "pending") {
      return refused(409, "approval_invalid", `Approval is ${approval.effective_status}`, "not_pending");
    }
    const decision = {
      schema_version: 1 as const,
      approval_id: submission.approval_id,
      action_digest: submission.action_digest,
      approver: approval.request.scope.principal,
      decided_at: "2026-10-29T10:05:00Z",
      decision: submission.decision,
      decision_id: `decision-${submission.approval_id}`,
      policy_version: approval.request.policy_version,
      reason: submission.reason,
    };
    if (isTerminal(stage!.view.record.status)) {
      const status = submission.decision === "approve" ? ("approved" as const) : ("rejected" as const);
      const request = { ...approval.request, status };
      stages.set(scenario.id, { ...stage!, approval: { ...approval, request, effective_status: status, decision } });
      const message = `Campaign is already ${stage!.view.record.status}`;
      const error = { code: "conflict" as const, message, ref: null, retryable: false, side_effect: "none" as const };
      return ok(
        { decision, approval: request, resumed: false, resume_error: { error, reason: "campaign_finished" } },
        [TEST_DOUBLE],
      );
    }
    if (scenario.resumeError !== undefined) {
      const status = submission.decision === "approve" ? ("approved" as const) : ("rejected" as const);
      const request = { ...approval.request, status };
      stages.set(scenario.id, {
        ...stage!,
        // A decided approval nothing has resumed: M07 reports this as stalled.
        view: { ...stage!.view, job: { state: "stalled", cancel_requested: false, error: null } },
        approval: { ...approval, request, effective_status: status, decision },
      });
      return ok({ decision, approval: request, resumed: false, resume_error: scenario.resumeError }, [TEST_DOUBLE]);
    }
    const next = submission.decision === "approve" ? scenario.approved : scenario.rejected;
    if (next === undefined || next.approval === null) return notFound("next stage");
    stages.set(scenario.id, { ...next, approval: { ...next.approval, decision } });
    return ok({ decision, approval: next.approval.request, resumed: true, resume_error: null }, [TEST_DOUBLE]);
  }

  return {
    kind: "fixture",
    harness: {
      scenarios,
      isOffline: () => offline,
      setOffline: (value) => {
        offline = value;
      },
    },
    getSession: () =>
      respond(null, () =>
        ok(
          {
            schema_version: 1,
            principal: { kind: "local_dev", principal_id: "local-dev-user" },
            project_id: "project-local",
            auth_mode: "local_dev",
            production_authenticated: false,
            can_approve: true,
            inference: "disabled",
            runnable_worlds: ["refund-world/1", "group-journey/1"],
          },
          [TEST_DOUBLE],
        ),
      ),
    createCampaign: () =>
      respond(null, () => refused(403, "forbidden", "Test data starts no campaigns", null)),
    getCampaign: (id) =>
      respond(id, () => {
        const failure = loadFailure(id);
        if (failure !== null) return failure;
        const stage = stageOf(id);
        return stage === undefined ? notFound("campaign") : ok(stage.view, stage.origins);
      }),
    listEvents: (id, after) =>
      respond(id, () => {
        const stage = stageOf(id);
        if (stage === undefined) return notFound("campaign");
        const rest = stage.events.filter((event) => event.sequence > after);
        const events = rest.slice(0, PAGE_SIZE);
        const page: EventPage = {
          campaign_id: id,
          events,
          next_after: events.at(-1)?.sequence ?? after,
          has_more: rest.length > events.length,
          inference: "fixture",
        };
        return ok(page, stage.origins);
      }),
    cancelCampaign: (id) =>
      respond(id, () => {
        const stage = stageOf(id);
        if (stage === undefined) return notFound("campaign");
        const next = scenarioOf(id)?.cancelled;
        if (next === undefined) return refused(409, "conflict", "This campaign has finished", "campaign_finished");
        return move(id, next);
      }),
    resumeCampaign: (id) =>
      respond(id, () => {
        const error = scenarioOf(id)?.resumeError;
        if (error !== undefined) return { ok: false, failure: { kind: "service", status: 409, ...error } };
        return move(id, scenarioOf(id)?.resumed);
      }),
    getExperiment: (id, experimentId) =>
      respond(id, () =>
        withExperiment(id, experimentId, (experiment) =>
          ok(
            {
              experiment_id: experiment.experiment_id,
              iteration: 1,
              proposal: experiment.proposal,
              execution: experiment.execution,
              diff: experiment.diff,
              findings: experiment.findings,
              runs: Object.keys(experiment.runs) as ExperimentView["runs"],
              coverage: experiment.coverage,
              inference: "fixture",
            },
            experiment.origins,
          ),
        ),
      ),
    getRun: (id, experimentId, name) =>
      respond(id, () =>
        withExperiment(id, experimentId, (experiment) => {
          const run = experiment.runs[name];
          if (run === undefined) return notFound(`${name} run`);
          return ok(
            {
              run_id: run.run_id,
              experiment_id: experimentId,
              run: name,
              artifact: { kind: "artifact", id: `artifact-${run.run_id}`, sha256: null },
              execution: name === "repaired" ? { mode: "enforced", guarded: true } : experiment.execution,
              result: run.result,
              findings: run.findings,
              inference: "fixture",
            },
            run.origins,
          );
        }),
      ),
    exportCapsule: (id, experimentId, name) =>
      respond(id, () =>
        withExperiment(id, experimentId, (experiment) => {
          if (name !== "fork") return notFound(`published capsule for the ${name} run`);
          exported.add(`${id}/${experimentId}/${name}`);
          const artifactId = `capsule-${id}-${experimentId}-${name}`;
          artifacts.set(artifactId, experiment.capsule);
          return ok(
            {
              artifact: { kind: "artifact", id: artifactId, sha256: experiment.capsule.sha256 },
              world_version: experiment.diff.world_version,
              capsule_schema_version: experiment.capsuleSchemaVersion,
              inference: "fixture",
            },
            experiment.capsuleOrigins,
          );
        }),
      ),
    replayCapsule: (id, experimentId, name) =>
      respond(id, () =>
        withExperiment(id, experimentId, (experiment) =>
          exported.has(`${id}/${experimentId}/${name}`)
            ? ok(experiment.replay, experiment.capsuleOrigins)
            : notFound(`exported capsule for the ${name} run`),
        ),
      ),
    downloadArtifact: (artifactId) =>
      respond(null, () => {
        const artifact = artifacts.get(artifactId);
        if (artifact === undefined) return notFound("artifact");
        return ok(new Blob([JSON.stringify(artifact, null, 2)], { type: "application/json" }), [TEST_DOUBLE]);
      }),
    getApproval: (approvalId) =>
      respond(null, () => {
        for (const scenario of scenarios) {
          const stage = stageOf(scenario.id);
          if (stage?.approval?.request.approval_id === approvalId) return ok(stage.approval, stage.origins);
        }
        return notFound("approval");
      }),
    decideApproval: (submission) => respond(null, () => decide(submission)),
    listExtEnvironments: () => respond(null, () => ok({ environments: EXT_ENVS }, [TEST_DOUBLE])),
    getExtEnvironment: (world) =>
      respond(null, () => {
        const found = EXT_ENVS.find((env) => env.world_version === world);
        if (found === undefined) return notFound("environment");
        const detail: ExtEnvironmentDetail = { ...found, tool_specs: [], fault_specs: [], package: "fixture", package_version: "fixture/1" };
        return ok(detail, [TEST_DOUBLE]);
      }),
    getExtFixture: (world, name) =>
      respond(null, () => {
        const found = EXT_ENVS.find((env) => env.world_version === world);
        if (found === undefined || !found.fixtures.includes(name)) return notFound("fixture");
        const fixture: ExtFixture = { world_version: world, name, snapshot: { fixture: name } };
        return ok(fixture, [TEST_DOUBLE]);
      }),
    qualifyExtension: () =>
      respond(null, () => {
        const report: ExtQualificationReport = { world_version: "refund-world/1", environment_id: "refund-environment", passed: true, checks: [{ name: "reset", passed: true, details: "fixture" }], missing_capabilities: [], notes: [], details: {} };
        return ok(report, [TEST_DOUBLE]);
      }),
    assessExtFidelity: () =>
      respond(null, () => {
        const report: ExtFidelityReport = { world_version: "refund-world/1", passed: true, determinism_identical: true, replay_reproduced: true, snapshot_supported: true, missing_capabilities: [], notes: [], details: {} };
        return ok(report, [TEST_DOUBLE]);
      }),
    runExtCampaign: () =>
      respond(null, () => {
        const report: ExtCampaignReport = { attempts_run: 0, tools_used: [], faults_fired: [], rules_failed: [], caps_respected: true, guided_excluded: true, details: {} };
        return ok(report, [TEST_DOUBLE]);
      }),
    inspectExtCapsule: () =>
      respond(null, () => {
        const report: ExtInspection = { schema_version: 2, world_version: "refund-world/1", replay_kind: "recorded", truncated: false, failed_invariants: [], external: [], replay_supported: true, replay_detail: "fixture", details: {} };
        return ok(report, [TEST_DOUBLE]);
      }),
    replayExtRecorded: () =>
      respond(null, () => {
        const report: ExtReplayResult = { reproduced: true, matches: { trace: true, final_state: true, checks: true }, divergence: null, tool_calls: 0, failed: [], witness_sha256: "fixture", details: {} };
        return ok(report, [TEST_DOUBLE]);
      }),
    retestExtFresh: () =>
      respond(null, () => {
        const report: ExtRetestResult = { status: "completed", tool_calls: 0, stopped: null, denied: false, denial_summary: null, target_adapter_id: "fixture", target_adapter_version: "fixture/1", failed: [], witness_sha256: "fixture", replay_equivalence_claimed: false, details: {} };
        return ok(report, [TEST_DOUBLE]);
      }),
    minimizeExtFaults: () =>
      respond(null, () => {
        const report: ExtMinimizeResult = { status: "already_minimal", selected_faults: [], removed_faults: [], runs_used: 1, cardinality_minimal: true, witness_sha256: "fixture", regression: {}, details: {} };
        return ok(report, [TEST_DOUBLE]);
      }),
  };
}
