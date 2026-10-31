/**
 * Service-backed tests against a running M07 service. They need M07_ORIGIN, and fail
 * loudly without it rather than passing by skipping. A pass against an unmerged M07
 * branch is not an integrated result, and scripted inference is not a live one.
 *
 * Start outcome_server.py, a thin wrapper over M07's fixture server (scripted replies,
 * never a model), then run `node tests/integration/run.mjs` from web/:
 *
 *   GHOSTLAB_AUTH_MODE=local_dev python tests/integration/outcome_server.py \
 *     --m07 <checkout of feature/m07-api> --root <dir> --port 8788
 *
 * Worlds, stores, approvals, verifier and capsules are M07's real code over real HTTP,
 * through the same client, poller and panels the interface uses.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import type {
  ApprovalView,
  CampaignView,
  ExperimentView,
  RunView,
  WorldCampaignSpec,
} from "../../src/api/api.generated.ts";
import { createApiClient } from "../../src/api/client.ts";
import type { DataSource, Failure, Result, RunName } from "../../src/data/source.ts";
import { SERVICE_ORIGIN } from "../../src/data/source.ts";
import { actionFacts, buildSubmission, IdempotencyKeys } from "../../src/logic/approval.ts";
import { coverageOf, readCoverage } from "../../src/logic/coverage.ts";
import { violatingEventIds } from "../../src/logic/evidence.ts";
import { describeFailure } from "../../src/logic/failure.ts";
import { presentReplay } from "../../src/logic/replay.ts";
import { campaignStatus } from "../../src/logic/status.ts";
import { CampaignLive } from "../../src/logic/live.ts";
import { presentRepair } from "../../src/logic/repair.ts";
import { ApprovalDetail } from "../../src/views/ApprovalPage.tsx";
import { CampaignPanel, RepairResult } from "../../src/views/CampaignPage.tsx";
import { ReplayPanel } from "../../src/views/CapsulePage.tsx";
import { DiffPanel } from "../../src/views/DiffPage.tsx";

const origin = process.env.M07_ORIGIN;
assert.ok(origin, "M07_ORIGIN is required: these tests must not pass without a service");

const client = createApiClient({ baseUrl: origin });
const nonce = Date.now().toString(36);
const noTimers = { setTimer: () => null, clearTimer: () => undefined };
const text = (markup: string) => markup.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");

function spec(world: WorldCampaignSpec["world_version"], marker = ""): WorldCampaignSpec {
  return {
    schema_version: 1,
    world_version: world,
    objective: `Find a failure after a fault ${marker}`.trim(),
    success_criteria: ["A failing invariant is reproduced and a repair is verified"],
    budget: {
      cost_currency: null,
      max_cost_minor: null,
      max_input_tokens: 100000,
      max_model_calls: 10,
      max_output_tokens: 20000,
      max_parallel_runs: 2,
      max_tool_calls: 100,
      max_wall_clock_ms: 1200000,
    },
  };
}

function unwrap<T>(result: Result<T>): T {
  if (!result.ok) assert.fail(`Unexpected failure: ${JSON.stringify(result.failure)}`);
  return result.data;
}

function failureOf<T>(result: Result<T>): Extract<Failure, { kind: "service" }> {
  assert.equal(result.ok, false);
  if (result.ok || result.failure.kind !== "service") assert.fail("expected a service failure");
  return result.failure;
}

async function until(live: CampaignLive, ...statuses: string[]): Promise<CampaignView> {
  const deadline = Date.now() + 30000;
  for (;;) {
    await live.refresh();
    const view = live.getState().view;
    if (view !== null && statuses.includes(view.record.status) && view.job.state !== "active") return view;
    if (Date.now() > deadline) assert.fail(`timed out in ${view?.record.status} / ${view?.job.state}`);
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

interface Paused {
  campaignId: string;
  live: CampaignLive;
  view: CampaignView;
  experiment: ExperimentView;
  baseline: RunView;
  fork: RunView;
  approval: ApprovalView;
}

async function pausedCampaign(
  source: DataSource,
  world: WorldCampaignSpec["world_version"],
  key: string,
  marker = "",
): Promise<Paused> {
  const created = unwrap(await source.createCampaign(spec(world, marker), `${key}-${nonce}`));
  const campaignId = created.record.campaign_id;
  const live = new CampaignLive(source, campaignId, noTimers);
  const view = await until(live, "awaiting_approval");
  const summary = view.experiments[0];
  assert.ok(summary);
  const experimentId = summary.experiment_id;
  const experiment = unwrap(await source.getExperiment(campaignId, experimentId));
  const run = async (name: RunName) => unwrap(await source.getRun(campaignId, experimentId, name));
  const approval = unwrap(await source.getApproval(view.approval?.approval_id ?? ""));
  return { campaignId, live, view, experiment, baseline: await run("baseline"), fork: await run("fork"), approval };
}

function diffText(paused: Paused): string {
  return text(
    renderToStaticMarkup(
      <DiffPanel
        campaignId={paused.campaignId}
        experiment={paused.experiment}
        origins={SERVICE_ORIGIN}
        baseline={paused.baseline}
        fork={paused.fork}
        runOrigins={SERVICE_ORIGIN}
      />,
    ),
  );
}

test("the session reports what the interface must show", async () => {
  const session = unwrap(await client.getSession());
  assert.equal(session.auth_mode, "local_dev");
  assert.equal(session.production_authenticated, false);
  assert.equal(session.inference, "fixture");
  assert.equal(session.can_approve, true);
});

test("refund: create, diff, replay, approve, verified repair", async () => {
  const paused = await pausedCampaign(client, "refund-world/1", "refund");
  const { campaignId, live, view, experiment, fork } = paused;
  const experimentId = experiment.experiment_id;

  const state = live.getState();
  assert.equal(state.cursor, view.event_count);
  assert.deepEqual(state.events.map((event) => event.sequence), state.events.map((_, index) => index + 1));

  assert.equal(experiment.diff.baseline_metrics.refunded_minor, 250000);
  assert.equal(experiment.diff.faulted_metrics.refunded_minor, 500000);
  assert.equal(experiment.diff.first_divergence?.event_id, 2);
  assert.equal(fork.result.checks.total_refund_within_payment, false);
  const coverage = coverageOf(experiment, paused.baseline, fork);
  assert.equal(coverage?.coverage.complete, true, "both scripted runs finish");
  assert.equal(coverage?.recorded, true, "the service reports coverage");
  assert.deepEqual(view.experiments[0]?.coverage, experiment.coverage);
  const page = diffText(paused);
  assert.match(page, /First divergence Event 2/);
  assert.match(page, /Total refunded 500000 minor units/);

  const capsule = unwrap(await client.exportCapsule(campaignId, experimentId, "fork"));
  assert.equal(capsule.capsule_schema_version, 1);
  const replay = unwrap(await client.replayCapsule(campaignId, experimentId, "fork"));
  assert.equal(replay.reproduced, true);
  assert.equal(replay.model_calls, 0);
  assert.equal(replay.checks.total_refund_within_payment, false);
  assert.match(renderToStaticMarkup(<ReplayPanel result={replay} origins={SERVICE_ORIGIN} />), /Failure reproduced/);
  const file = unwrap(await client.downloadArtifact(capsule.artifact.id));
  assert.ok((await file.text()).includes("trusted-synthetic-refund-adapter"));

  const { approval } = paused;
  assert.equal(approval.effective_status, "pending");
  assert.equal(approval.action?.adapter, "repaired");
  assert.equal(actionFacts(approval.action).mode, null, "refund's action has no execution mode");
  assert.ok(actionFacts(approval.action).fixtureSha256);
  assert.equal(view.fixture_sha256, actionFacts(approval.action).fixtureSha256, "the digest the approval binds");
  const detail = text(renderToStaticMarkup(<ApprovalDetail approval={approval} origins={SERVICE_ORIGIN} />));
  assert.match(detail, new RegExp(approval.request.action_digest));

  const forged = { ...buildSubmission(approval, "approve", ""), action_digest: "0".repeat(64) };
  const refused = failureOf(await client.decideApproval(forged, `forged-${nonce}`));
  assert.notEqual(describeFailure(refused).state, "reconnecting");
  const still = unwrap(await client.getApproval(approval.request.approval_id));
  assert.equal(still.effective_status, "pending", "a forged digest changes nothing");

  const keys = new IdempotencyKeys();
  const submission = buildSubmission(approval, "approve", "Reviewed the diff");
  const first = unwrap(await client.decideApproval(submission, keys.keyFor(submission)));
  assert.equal(first.resumed, true);
  assert.equal(first.decision.approver.principal_id, approval.request.scope.principal.principal_id);
  const retried = unwrap(await client.decideApproval(submission, keys.keyFor(submission)));
  assert.equal(retried.decision.decision_id, first.decision.decision_id, "a retry records no second decision");

  const done = await until(live, "completed", "failed", "budget_exhausted");
  assert.equal(done.outcome, "repair_verified");
  assert.ok(done.repair);
  assert.equal(done.repair.refunded_minor, 250000);
  assert.equal(presentRepair(done.repair, done.outcome, "refund-world/1").tone, "ok");
  assert.equal(done.repair.held_by, null);

  const repaired = unwrap(await client.getRun(campaignId, experimentId, "repaired"));
  assert.equal(repaired.result.checks.total_refund_within_payment, true);
  unwrap(await client.exportCapsule(campaignId, experimentId, "repaired"));
  const clean = unwrap(await client.replayCapsule(campaignId, experimentId, "repaired"));
  assert.equal(clean.reproduced, true);
  assert.deepEqual(clean.metrics, { refunded_minor: 250000 });
  const used = unwrap(await client.getApproval(approval.request.approval_id));
  assert.equal(used.effective_status, "consumed");
});

test("journey: divergence and violation differ, and the repair is recorded", async () => {
  const paused = await pausedCampaign(client, "group-journey/1", "journey");
  const { campaignId, live, experiment, approval } = paused;
  const firstDivergence = experiment.diff.first_divergence?.event_id;
  const violating = violatingEventIds(experiment.findings);
  assert.ok(firstDivergence);
  assert.ok(violating.length > 0);
  assert.ok(!violating.includes(firstDivergence), "the two must be different events");
  assert.deepEqual(experiment.diff.violated_invariants, ["delivered_messages_permitted"]);
  assert.deepEqual(experiment.execution, { mode: "lab", guarded: false });
  const page = diffText(paused);
  assert.match(page, new RegExp(`First divergence Event ${firstDivergence}`));
  assert.match(page, new RegExp(`Violation evidence Event ${violating[0]}`));
  assert.match(page, /messages_delivered_without_permission/);

  const facts = actionFacts(approval.action);
  assert.equal(facts.mode, "enforced");
  assert.equal(facts.guarded, true);
  assert.match(facts.fixtureSha256 ?? "", /^[0-9a-f]{64}$/);

  const capsule = unwrap(await client.exportCapsule(campaignId, experiment.experiment_id, "fork"));
  assert.equal(capsule.capsule_schema_version, 2);
  const replay = unwrap(await client.replayCapsule(campaignId, experiment.experiment_id, "fork"));
  assert.equal(replay.reproduced, true);
  assert.equal(replay.model_calls, 0);
  assert.equal(replay.guarded, false);

  const submission = buildSubmission(approval, "approve", "");
  unwrap(await client.decideApproval(submission, `journey-decision-${nonce}`));
  const done = await until(live, "completed", "failed", "budget_exhausted");
  assert.ok(done.repair);
  assert.equal(done.outcome, "repair_verified");
  assert.equal(done.repair.execution?.mode, "enforced");
  assert.equal(done.repair.held_by, "adapter");
  assert.equal(done.repair.task_complete, true);
  const shown = text(
    renderToStaticMarkup(
      <RepairResult repair={done.repair} outcome={done.outcome} worldVersion="group-journey/1" origins={SERVICE_ORIGIN} />,
    ),
  );
  assert.match(shown, /run_status completed .*safety_held true .*task_complete true .*verified true .*held_by adapter/);
});

test("rejecting ends the campaign without a repair, and 'completed' is not success", async () => {
  const { live, approval } = await pausedCampaign(client, "refund-world/1", "reject");
  const submission = buildSubmission(approval, "reject", "Not this adapter");
  unwrap(await client.decideApproval(submission, `reject-decision-${nonce}`));
  const done = await until(live, "completed", "failed");
  assert.equal(done.record.status, "completed");
  assert.equal(done.outcome, "repair_rejected");
  assert.equal(done.repair, null);
});

test("cancelling a paused campaign ends it, and a later decision moves nothing", async () => {
  const { campaignId, live, approval } = await pausedCampaign(client, "refund-world/1", "cancel");
  unwrap(await client.cancelCampaign(campaignId));
  const done = await until(live, "cancelled", "completed", "failed");
  assert.equal(done.record.status, "cancelled");
  assert.equal(done.repair, null);
  assert.equal(unwrap(await client.getApproval(approval.request.approval_id)).effective_status, "pending");

  const decided = unwrap(await client.decideApproval(buildSubmission(approval, "approve", ""), `after-cancel-${nonce}`));
  assert.equal(decided.resumed, false);
  assert.equal(decided.resume_error?.reason, "campaign_finished");
  await live.refresh();
  assert.equal(live.getState().view?.record.status, "cancelled");
  assert.equal(live.getState().view?.repair, null);
  const finished = failureOf(await client.cancelCampaign(`missing-${nonce}`));
  assert.equal(describeFailure(finished).state, "not_found");
});

test("coverage_incomplete can mean a fault never fired, with both runs finished", async () => {
  const created = unwrap(await client.createCampaign(spec("refund-world/1", "outcome-silent-fault"), `silent-${nonce}`));
  const live = new CampaignLive(client, created.record.campaign_id, noTimers);
  const view = await until(live, "budget_exhausted", "completed", "failed");
  assert.equal(view.outcome, "coverage_incomplete");
  assert.deepEqual(view.experiments[0]?.coverage, {
    baseline: "completed",
    fork: "completed",
    faults_triggered: false,
    complete: false,
  });
  const page = renderToStaticMarkup(<CampaignPanel view={view} origins={SERVICE_ORIGIN} source={client} refresh={async () => undefined} />);
  assert.match(text(page), /faults_triggered false/);
  assert.doesNotMatch(page, /data-tone="ok"/);
});

test("no_experiment_ran is a failure, not a clean result", async () => {
  const created = unwrap(await client.createCampaign(spec("refund-world/1", "outcome-no-experiment"), `none-${nonce}`));
  const live = new CampaignLive(client, created.record.campaign_id, noTimers);
  const view = await until(live, "failed", "completed", "budget_exhausted");
  assert.equal(view.record.status, "failed");
  assert.equal(view.outcome, "no_experiment_ran");
  assert.deepEqual(view.experiments, []);
  const page = renderToStaticMarkup(<CampaignPanel view={view} origins={SERVICE_ORIGIN} source={client} refresh={async () => undefined} />);
  assert.match(text(page), /No experiment ran/);
  assert.doesNotMatch(page, /data-tone="ok"/);
});

test("budget_check_failed is not reported as exhaustion", async () => {
  const created = unwrap(await client.createCampaign(spec("group-journey/1", "outcome-budget-check"), `budget-${nonce}`));
  const live = new CampaignLive(client, created.record.campaign_id, noTimers);
  const view = await until(live, "failed", "completed", "budget_exhausted");
  assert.equal(view.record.status, "failed");
  assert.equal(view.outcome, "budget_check_failed");
  assert.equal(view.errors.at(-1)?.code, "internal");
});

test("a repair that missed a fault the fork met shows lost_faults, not success", async () => {
  const { live, approval } = await pausedCampaign(client, "group-journey/1", "lost-fault", "outcome-lost-fault");
  unwrap(await client.decideApproval(buildSubmission(approval, "approve", ""), `lost-decision-${nonce}`));
  const done = await until(live, "failed", "completed", "budget_exhausted");
  assert.equal(done.record.status, "failed");
  assert.equal(done.outcome, "repair_incomplete");
  assert.ok(done.repair);
  assert.deepEqual(done.repair.lost_faults, ["sharing_revoked_while_queued"]);
  assert.equal(done.repair.held_by, null);
  assert.equal(presentRepair(done.repair, done.outcome, "group-journey/1").tone, "warn");
});

test("a bad spec is a validation error and creates nothing", async () => {
  const bad = { ...spec("refund-world/1"), budget: { ...spec("refund-world/1").budget, max_tool_calls: -1 } };
  const failure = failureOf(await client.createCampaign(bad, `invalid-${nonce}`));
  assert.equal(failure.status, 422);
  assert.equal(describeFailure(failure).state, "validation_error");
});

test("coverage_incomplete: a truncated run with no failure is never success", async () => {
  const created = unwrap(await client.createCampaign(spec("group-journey/1", "outcome-truncated"), `truncated-${nonce}`));
  const campaignId = created.record.campaign_id;
  const live = new CampaignLive(client, campaignId, noTimers);
  const view = await until(live, "budget_exhausted", "completed", "failed");
  assert.equal(view.record.status, "budget_exhausted");
  assert.equal(view.outcome, "coverage_incomplete");
  assert.equal(view.approval, null);
  assert.equal(view.repair, null);
  assert.equal(campaignStatus(view.record.status, view.outcome).tone, "warn");
  const summary = view.experiments.at(-1)!;
  assert.equal(readCoverage(summary)?.complete, false);
  const page = renderToStaticMarkup(<CampaignPanel view={view} origins={SERVICE_ORIGIN} source={client} refresh={async () => undefined} />);
  assert.match(text(page), /this proves nothing/);
  assert.match(text(page), /complete false/);
  assert.doesNotMatch(page, /data-tone="ok"/);

  const experimentId = summary.experiment_id;
  const experiment = unwrap(await client.getExperiment(campaignId, experimentId));
  const baseline = unwrap(await client.getRun(campaignId, experimentId, "baseline"));
  const fork = unwrap(await client.getRun(campaignId, experimentId, "fork"));
  assert.equal(fork.result.status, "budget_exhausted");
  const diff = text(
    renderToStaticMarkup(
      <DiffPanel campaignId={campaignId} experiment={experiment} origins={SERVICE_ORIGIN} baseline={baseline} fork={fork} runOrigins={SERVICE_ORIGIN} />,
    ),
  );
  assert.match(diff, /Coverage incomplete/);
  assert.doesNotMatch(diff, /read from the two recorded runs/);

  unwrap(await client.exportCapsule(campaignId, experimentId, "fork"));
  const replay = unwrap(await client.replayCapsule(campaignId, experimentId, "fork"));
  assert.equal(replay.reproduced, true);
  assert.equal(replay.run_status, "budget_exhausted");
  assert.equal(presentReplay(replay).tone, "warn");
});

test("a violation before truncation stands and still reaches approval", async () => {
  const paused = await pausedCampaign(client, "group-journey/1", "violation", "outcome-violation");
  assert.equal(readCoverage(paused.experiment)?.complete, false);
  assert.deepEqual(violatingEventIds(paused.experiment.findings), [10]);
  assert.equal(paused.approval.effective_status, "pending");
  assert.match(diffText(paused), /Coverage incomplete, violation recorded/);
  assert.match(diffText(paused), /Violation evidence Event 10/);
});

test("repair_incomplete: a truncated repair is not verified and held_by stays null", async () => {
  const { live, approval, campaignId, experiment } = await pausedCampaign(client, "group-journey/1", "short-repair", "outcome-short-repair");
  unwrap(await client.decideApproval(buildSubmission(approval, "approve", ""), `short-repair-decision-${nonce}`));
  const done = await until(live, "budget_exhausted", "completed", "failed");
  assert.equal(done.record.status, "budget_exhausted");
  assert.equal(done.outcome, "repair_incomplete");
  assert.ok(done.repair);
  assert.equal(done.repair.held_by, null);
  assert.equal(done.repair.safety_held, true);
  const shown = presentRepair(done.repair, done.outcome, "group-journey/1");
  assert.equal(shown.tone, "warn");
  assert.equal(shown.headline, "Repair incomplete");
  unwrap(await client.exportCapsule(campaignId, experiment.experiment_id, "repaired"));
  const replay = unwrap(await client.replayCapsule(campaignId, experiment.experiment_id, "repaired"));
  assert.equal(replay.run_status, "budget_exhausted");
  assert.equal(presentReplay(replay).tone, "warn");
});

test("settings_changed at consumption ends the campaign, approval unspent", async () => {
  const { live, approval, campaignId } = await pausedCampaign(client, "group-journey/1", "at-consume", "outcome-settings-at-consume");
  const decided = unwrap(await client.decideApproval(buildSubmission(approval, "approve", ""), `at-consume-decision-${nonce}`));
  assert.equal(decided.resumed, true);
  const done = await until(live, "failed", "completed", "budget_exhausted");
  assert.equal(done.record.status, "failed");
  assert.equal(done.outcome, "settings_changed");
  assert.equal(done.repair, null);
  assert.equal(campaignStatus(done.record.status, done.outcome).label, "Blocked: settings changed");
  const after = unwrap(await client.getApproval(approval.request.approval_id));
  assert.equal(after.effective_status, "approved", "not spent");
  const page = renderToStaticMarkup(<CampaignPanel view={done} origins={SERVICE_ORIGIN} source={client} refresh={async () => undefined} />);
  assert.match(page, /data-testid="state-blocked"/);
  assert.doesNotMatch(page, /data-testid="state-failed"/);
  assert.doesNotMatch(text(page), /Resume campaign/);
  const resumed = unwrap(await client.resumeCampaign(campaignId));
  assert.equal(resumed.record.status, "failed", "resume does nothing here, so the UI offers none");
});

test("a resume refused by settings_changed is blocked, and restoring them lets it finish", async () => {
  const { live, approval, campaignId } = await pausedCampaign(client, "group-journey/1", "at-resume", "outcome-settings-at-resume");
  const decided = unwrap(await client.decideApproval(buildSubmission(approval, "approve", ""), `at-resume-decision-${nonce}`));
  assert.equal(decided.resumed, false);
  assert.equal(decided.resume_error?.reason, "settings_changed");
  assert.equal(decided.resume_error?.error.code, "conflict");
  const failure: Failure = { kind: "service", status: 409, ...decided.resume_error! };
  assert.equal(describeFailure(failure).state, "blocked");

  const held = await until(live, "awaiting_approval");
  assert.equal(held.repair, null);
  assert.equal(held.job.state, "stalled");
  assert.equal(unwrap(await client.getApproval(approval.request.approval_id)).effective_status, "approved");

  unwrap(await client.resumeCampaign(campaignId));
  const done = await until(live, "completed", "failed", "budget_exhausted");
  assert.equal(done.outcome, "repair_verified");
  assert.equal(unwrap(await client.getApproval(approval.request.approval_id)).effective_status, "consumed");
});

test("a withdrawn binding refuses the resume, leaves the approval unspent, and restoring it finishes", async () => {
  const { live, approval, campaignId } = await pausedCampaign(client, "group-journey/1", "binding", "outcome-binding-at-resume");
  const decided = unwrap(await client.decideApproval(buildSubmission(approval, "approve", ""), `binding-decision-${nonce}`));
  assert.equal(decided.resumed, false);
  assert.equal(decided.resume_error?.reason, "settings_changed");
  assert.equal(decided.resume_error?.error.message, "the world identity no longer owns this");
  const failure: Failure = { kind: "service", status: 409, ...decided.resume_error! };
  assert.equal(describeFailure(failure).state, "blocked");
  const held = await until(live, "awaiting_approval");
  assert.equal(held.repair, null, "no repair ran, so none can read as failed");
  assert.equal(unwrap(await client.getApproval(approval.request.approval_id)).effective_status, "approved");

  unwrap(await client.resumeCampaign(campaignId));
  const done = await until(live, "completed", "failed", "budget_exhausted");
  assert.equal(done.outcome, "repair_verified");
  assert.equal(unwrap(await client.getApproval(approval.request.approval_id)).effective_status, "consumed");
});

test("credential-shaped client text is refused, stores nothing and approves nothing", async () => {
  const objective = { ...spec("refund-world/1"), objective: "Check token=abcd1234 handling" };
  const created = failureOf(await client.createCampaign(objective, `secret-${nonce}`));
  assert.equal(created.status, 422);
  assert.equal(created.reason, "secret_shaped_text");
  assert.equal(describeFailure(created).state, "validation_error");
  assert.doesNotMatch(created.error.message, /abcd1234/);

  const { approval } = await pausedCampaign(client, "refund-world/1", "secret-reason");
  const refused = failureOf(
    await client.decideApproval(buildSubmission(approval, "approve", "token=abcd1234"), `secret-decision-${nonce}`),
  );
  assert.equal(refused.reason, "secret_shaped_text");
  assert.equal(unwrap(await client.getApproval(approval.request.approval_id)).effective_status, "pending");
});

test("a design-only world is refused", async () => {
  const failure = failureOf(await client.createCampaign(spec("service-relay/1"), `relay-${nonce}`));
  assert.equal(failure.status, 403);
  assert.equal(describeFailure(failure).state, "denied");
});

test("an unknown campaign is not found, and polling stops", async () => {
  const live = new CampaignLive(client, `missing-${nonce}`, noTimers);
  await live.refresh();
  const state = live.getState();
  assert.equal(state.phase, "failed");
  assert.equal(state.failure === null ? null : describeFailure(state.failure).state, "not_found");
});

test("reconnecting re-reads the campaign first, over real HTTP", async () => {
  let online = true;
  const paths: string[] = [];
  const flaky = createApiClient({
    baseUrl: origin,
    fetch: async (input, init) => {
      if (!online) throw new TypeError("connection refused");
      paths.push(String(input).replace(origin, ""));
      return globalThis.fetch(input, init);
    },
  });
  const { campaignId, live, view } = await pausedCampaign(flaky, "refund-world/1", "reconnect");
  const lastEvents = live.getState().events.length;
  assert.ok(lastEvents > 0);

  online = false;
  await live.refresh();
  assert.equal(live.getState().connection, "reconnecting");
  assert.equal(live.getState().view?.record.status, "awaiting_approval");

  online = true;
  paths.length = 0;
  await live.refresh();
  assert.equal(live.getState().connection, "live");
  assert.equal(paths[0], `/v1/campaigns/${campaignId}`, "authoritative state comes first");
  assert.equal(paths.length, 1, "the cursor is current, so no events are re-read");
  assert.equal(live.getState().events.length, lastEvents, "nothing is duplicated");
  assert.equal(live.getState().cursor, view.event_count);
});
