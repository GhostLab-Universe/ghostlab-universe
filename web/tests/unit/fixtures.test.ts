import assert from "node:assert/strict";
import { test } from "node:test";
import { createFixtureSource } from "../../src/data/fixtureSource.ts";
import { isTestData } from "../../src/data/source.ts";
import { SCENARIOS } from "../../src/fixtures/scenarios.ts";
import { CampaignLive } from "../../src/logic/live.ts";
import { buildSubmission } from "../../src/logic/approval.ts";

const noTimers = { setTimer: () => null, clearTimer: () => undefined };

test("every scenario's data is marked as test data, never as service data", async () => {
  const source = createFixtureSource();
  for (const scenario of SCENARIOS) {
    if (scenario.loadFailure || scenario.delayMs) continue;
    const view = await source.getCampaign(scenario.id);
    assert.ok(view.ok, scenario.id);
    assert.ok(isTestData(view.origins), scenario.id);
  }
});

test("scenario ids are unique and events stay inside their campaign", () => {
  assert.equal(new Set(SCENARIOS.map((scenario) => scenario.id)).size, SCENARIOS.length);
  for (const { id, stage } of SCENARIOS) {
    assert.equal(stage.view.record.campaign_id, id);
    assert.equal(stage.view.event_count, stage.events.length, id);
    assert.deepEqual(
      stage.events.map((event) => event.sequence),
      stage.events.map((_, index) => index + 1),
      id,
    );
    for (const event of stage.events) assert.equal(event.campaign_id, id);
  }
});

test("the refund flow runs from evidence through approval to a verified repair", async () => {
  const source = createFixtureSource();
  const live = new CampaignLive(source, "fx-refund", noTimers);
  await live.refresh();
  assert.equal(live.getState().view?.record.status, "awaiting_approval");

  const experiment = await source.getExperiment("fx-refund", "experiment-1");
  assert.ok(experiment.ok);
  assert.equal(experiment.data.diff.baseline_metrics.refunded_minor, 250000);
  assert.equal(experiment.data.diff.faulted_metrics.refunded_minor, 500000);

  const capsule = await source.exportCapsule("fx-refund", "experiment-1", "fork");
  assert.ok(capsule.ok);
  assert.equal(capsule.data.capsule_schema_version, 1);
  const replay = await source.replayCapsule("fx-refund", "experiment-1", "fork");
  assert.ok(replay.ok);
  assert.equal(replay.data.reproduced, true);
  assert.equal(replay.data.model_calls, 0);
  assert.equal(replay.data.checks.total_refund_within_payment, false, "a reproduced failure stays a failure");

  const approvalId = live.getState().view!.approval!.approval_id;
  const approval = await source.getApproval(approvalId);
  assert.ok(approval.ok);
  const decided = await source.decideApproval(buildSubmission(approval.data, "approve", "Reviewed"), "key-1");
  assert.ok(decided.ok);
  assert.equal(decided.data.decision.reason, "Reviewed");

  await live.refresh();
  const view = live.getState().view!;
  assert.equal(view.record.status, "completed");
  assert.equal(view.outcome, "repair_verified");
  assert.equal(view.repair?.refunded_minor, 250000);
  const repaired = await source.getRun("fx-refund", "experiment-1", "repaired");
  assert.ok(repaired.ok);
  assert.equal(repaired.data.result.adapter_id, "repaired");
});

test("the journey flow reaches a repair held by the adapter", async () => {
  const source = createFixtureSource();
  const live = new CampaignLive(source, "fx-journey", noTimers);
  await live.refresh();
  const approvalId = live.getState().view!.approval!.approval_id;
  const approval = await source.getApproval(approvalId);
  assert.ok(approval.ok);
  const decided = await source.decideApproval(buildSubmission(approval.data, "approve", ""), "key-1");
  assert.ok(decided.ok);
  await live.refresh();
  const view = live.getState().view!;
  assert.equal(view.outcome, "repair_verified");
  assert.equal(view.repair?.held_by, "adapter");
  assert.equal(view.repair?.execution?.mode, "enforced");
});

test("rejecting ends the campaign as completed / repair_rejected and runs nothing", async () => {
  const source = createFixtureSource();
  const view = await source.getCampaign("fx-refund");
  assert.ok(view.ok);
  const approval = await source.getApproval(view.data.approval!.approval_id);
  assert.ok(approval.ok);
  await source.decideApproval(buildSubmission(approval.data, "reject", "Not this adapter"), "key-1");
  const after = await source.getCampaign("fx-refund");
  assert.ok(after.ok);
  assert.equal(after.data.record.status, "completed");
  assert.equal(after.data.outcome, "repair_rejected");
  assert.equal(after.data.repair, null);
  const repaired = await source.getRun("fx-refund", "experiment-1", "repaired");
  assert.equal(repaired.ok, false);
});

test("a second decision on a used approval is refused", async () => {
  const source = createFixtureSource();
  const view = await source.getCampaign("fx-refund");
  assert.ok(view.ok);
  const approval = await source.getApproval(view.data.approval!.approval_id);
  assert.ok(approval.ok);
  const submission = buildSubmission(approval.data, "approve", "");
  assert.ok((await source.decideApproval(submission, "key-1")).ok);
  assert.equal((await source.decideApproval(submission, "key-2")).ok, false);
});

test("replay needs an exported capsule, and only the published one exists", async () => {
  const source = createFixtureSource();
  assert.equal((await source.replayCapsule("fx-journey", "experiment-1", "fork")).ok, false);
  assert.equal((await source.exportCapsule("fx-journey", "experiment-1", "baseline")).ok, false);
  const capsule = await source.exportCapsule("fx-journey", "experiment-1", "fork");
  assert.ok(capsule.ok);
  assert.equal(capsule.data.capsule_schema_version, 2);
  const replay = await source.replayCapsule("fx-journey", "experiment-1", "fork");
  assert.ok(replay.ok);
  assert.equal(replay.data.guarded, false);
  assert.equal(replay.data.metrics.messages_delivered_without_permission, 1);
  const file = await source.downloadArtifact(capsule.data.artifact.id);
  assert.ok(file.ok);
  const parsed = JSON.parse(await file.data.text()) as { sha256: string };
  assert.equal(parsed.sha256, capsule.data.artifact.sha256);
});

test("cancelling follows the contract's refusals", async () => {
  const source = createFixtureSource();
  const finished = await source.cancelCampaign("fx-journey-truncated");
  assert.equal(finished.ok, false);
  if (!finished.ok && finished.failure.kind === "service") assert.equal(finished.failure.reason, "campaign_finished");
  const cancelled = await source.cancelCampaign("fx-running");
  assert.ok(cancelled.ok);
  assert.equal(cancelled.data.record.status, "cancelled");
});

test("resuming a stalled campaign reactivates it and grants nothing else", async () => {
  const source = createFixtureSource();
  const before = await source.getCampaign("fx-stalled");
  assert.ok(before.ok);
  assert.equal(before.data.job.state, "stalled");
  const after = await source.resumeCampaign("fx-stalled");
  assert.ok(after.ok);
  assert.equal(after.data.job.state, "active");
});

test("the harness can take the connection down and back", async () => {
  const source = createFixtureSource();
  source.harness.setOffline(true);
  const down = await source.getCampaign("fx-running");
  assert.equal(down.ok, false);
  if (!down.ok) assert.equal(down.failure.kind, "transport");
  source.harness.setOffline(false);
  assert.equal((await source.getCampaign("fx-running")).ok, true);
});
