import assert from "node:assert/strict";
import { test } from "node:test";
import type { CampaignView } from "../../src/api/api.generated.ts";
import { createFixtureSource } from "../../src/data/fixtureSource.ts";
import type { Failure } from "../../src/data/source.ts";
import { recorded } from "../../src/fixtures/recorded.generated.ts";
import { SCENARIOS } from "../../src/fixtures/scenarios.ts";
import { buildSubmission } from "../../src/logic/approval.ts";
import { coverageNote, coverageOf, readCoverage } from "../../src/logic/coverage.ts";
import { violatingEventIds } from "../../src/logic/evidence.ts";
import { BLOCKED_HELP, CAMPAIGN_FINISHED, describeFailure, failureState, revokesAccess } from "../../src/logic/failure.ts";
import { presentReplay } from "../../src/logic/replay.ts";
import { presentRepair } from "../../src/logic/repair.ts";
import { campaignStatus, outcomeNotice } from "../../src/logic/status.ts";

const stage = (id: string) => SCENARIOS.find((scenario) => scenario.id === id)!.stage;

const settingsChanged: Failure = {
  kind: "service",
  status: 409,
  reason: "settings_changed",
  error: { code: "conflict", message: "the world fixture changed", ref: null, retryable: false, side_effect: "none" },
};

test("recorded truncation is exactly what M05 asserts", () => {
  const clean = recorded.journey_truncated_clean;
  assert.deepEqual(clean.coverage, {
    baseline: "budget_exhausted",
    fork: "budget_exhausted",
    faults_triggered: false,
    complete: false,
  });
  assert.deepEqual(violatingEventIds(clean.findings), []);
  const violation = recorded.journey_truncated_violation;
  assert.equal(violation.coverage.complete, false);
  assert.deepEqual(violatingEventIds(violation.findings), [10]);
  assert.equal(violation.diff.first_divergence?.event_id, 3);
});

test("a complete coverage record needs no notice", () => {
  assert.equal(coverageNote({ baseline: "completed", fork: "completed", faults_triggered: true, complete: true }, 0), null);
});

test("truncation with no failure is never a pass", () => {
  const note = coverageNote(recorded.journey_truncated_clean.coverage, 0);
  assert.equal(note?.tone, "warn");
  assert.match(note?.detail ?? "", /proves nothing/);
  assert.match(note?.detail ?? "", /not a pass/);
});

test("a violation before truncation stands", () => {
  const note = coverageNote(recorded.journey_truncated_violation.coverage, 1);
  assert.equal(note?.tone, "warn");
  assert.match(note?.headline ?? "", /violation recorded/);
  assert.match(note?.detail ?? "", /it did reach stands/);
});

test("coverage that claims complete over an unfinished run reads as incomplete", () => {
  const coverage = { baseline: "completed" as const, fork: "budget_exhausted" as const, faults_triggered: true, complete: true };
  assert.equal(readCoverage({ coverage })?.complete, false);
});

test("coverage that claims complete when a fault never fired reads as incomplete", () => {
  const coverage = { baseline: "completed" as const, fork: "completed" as const, faults_triggered: false, complete: true };
  assert.equal(readCoverage({ coverage })?.complete, false);
});

test("a fault that never fired is said to be that, not a run that stopped", () => {
  const { coverage, replay } = recorded.journey_unfired;
  assert.deepEqual(coverage, { baseline: "completed", fork: "completed", faults_triggered: false, complete: false });
  const note = coverageNote(coverage, 0);
  assert.match(note?.detail ?? "", /Both runs finished, but a fault never fired/);
  assert.doesNotMatch(note?.detail ?? "", /stopped early/);
  assert.match(note?.detail ?? "", /not a pass/);
  assert.equal(replay.run_status, "completed");
});

test("a run that stopped and a fault that never fired are both named", () => {
  const note = coverageNote(recorded.journey_truncated_clean.coverage, 0);
  assert.match(note?.detail ?? "", /A run stopped early and a fault never fired/);
});

test("unknown fault coverage on an older checkpoint isn't reported as unfired", () => {
  const coverage = { baseline: "completed" as const, fork: "completed" as const, faults_triggered: null, complete: true };
  assert.equal(readCoverage({ coverage })?.complete, true);
});

for (const [outcome, title] of [
  ["no_experiment_ran", "No experiment ran"],
  ["budget_check_failed", "Budget check failed"],
] as const) {
  test(`${outcome} is a named failure, not a clean result`, () => {
    assert.equal(outcomeNotice(outcome)?.state, "failed");
    assert.equal(outcomeNotice(outcome)?.title, title);
    assert.equal(campaignStatus("failed", outcome).tone, "fail");
  });
}

test("other outcomes need no extra notice", () => {
  assert.equal(outcomeNotice("repair_verified"), null);
  assert.equal(outcomeNotice(null), null);
});

test("identifier_too_long is a validation error with its reason", () => {
  const failure: Failure = {
    kind: "service",
    status: 422,
    reason: "identifier_too_long",
    error: { code: "validation_failed", message: "nothing was stored", ref: null, retryable: false, side_effect: "none" },
  };
  assert.equal(failureState(failure), "validation_error");
  assert.equal(describeFailure(failure).reason, "identifier_too_long");
});

test("cancelling a paused campaign ends it and leaves the approval unspent", async () => {
  const source = createFixtureSource();
  const view = await source.getCampaign("fx-refund");
  assert.ok(view.ok);
  const cancelled = await source.cancelCampaign("fx-refund");
  assert.ok(cancelled.ok);
  assert.equal(cancelled.data.record.status, "cancelled");
  const approval = await source.getApproval(view.data.approval!.approval_id);
  assert.ok(approval.ok);
  assert.equal(approval.data.effective_status, "pending");
});

test("a decision on a stopped campaign is recorded and moves nothing", async () => {
  const source = createFixtureSource();
  const view = await source.getCampaign("fx-journey");
  assert.ok(view.ok);
  const id = view.data.approval!.approval_id;
  await source.cancelCampaign("fx-journey");
  const approval = await source.getApproval(id);
  assert.ok(approval.ok);
  const decided = await source.decideApproval(buildSubmission(approval.data, "approve", ""), "key-1");
  assert.ok(decided.ok);
  assert.equal(decided.data.resumed, false);
  assert.equal(decided.data.resume_error?.reason, CAMPAIGN_FINISHED);
  const after = await source.getCampaign("fx-journey");
  assert.ok(after.ok);
  assert.equal(after.data.record.status, "cancelled");
  assert.equal(after.data.repair, null);
  const recordedApproval = await source.getApproval(id);
  assert.ok(recordedApproval.ok);
  assert.equal(recordedApproval.data.decision?.decision, "approve");
});

test("coverage missing on an older checkpoint is never assumed complete", () => {
  assert.equal(readCoverage({ coverage: null }), null);
});

test("a withdrawn binding is a refusal, with the approval unspent", async () => {
  const source = createFixtureSource();
  const view = await source.getCampaign("fx-binding-withdrawn");
  assert.ok(view.ok);
  const id = view.data.approval!.approval_id;
  const approval = await source.getApproval(id);
  assert.ok(approval.ok);
  const decided = await source.decideApproval(buildSubmission(approval.data, "approve", ""), "key-1");
  assert.ok(decided.ok);
  assert.equal(decided.data.resumed, false);
  assert.equal(decided.data.resume_error?.error.message, "the world identity no longer owns this");
  const after = await source.getApproval(id);
  assert.ok(after.ok);
  assert.equal(after.data.effective_status, "approved");
  const campaign = await source.getCampaign("fx-binding-withdrawn");
  assert.ok(campaign.ok);
  assert.equal(campaign.data.repair, null);
});

test("blocked copy never reads as a failed repair", () => {
  assert.match(BLOCKED_HELP, /a refusal, not a failed repair/);
  assert.match(BLOCKED_HELP, /bound identity/);
  assert.match(outcomeNotice("settings_changed")?.detail ?? "", /not a failed repair/);
  assert.doesNotMatch(outcomeNotice("settings_changed")?.detail ?? "", /fixture changed/);
});

test("BLOCKED_HELP promises nothing a damaged record can't deliver", () => {
  assert.match(BLOCKED_HELP, /A damaged record can't be restored/);
  assert.doesNotMatch(BLOCKED_HELP, /lets the campaign resume\.$/);
});

test("replay: reproduced alone is never a pass", () => {
  const replay = recorded.journey_truncated_clean.replay;
  assert.equal(replay.reproduced, true);
  assert.equal(replay.run_status, "budget_exhausted");
  assert.equal(presentReplay(replay).tone, "warn");
  assert.match(presentReplay(replay).headline, /stopped early/);
  const violation = recorded.journey_truncated_violation.replay;
  assert.match(presentReplay(violation).detail, /still stands/);
  assert.equal(presentReplay(recorded.refund_replay).headline, "Failure reproduced");
  assert.equal(presentReplay({ ...recorded.refund_replay, reproduced: false }).tone, "fail");
  const clean = { ...recorded.refund_replay, checks: { a: true } };
  assert.equal(presentReplay(clean).tone, "neutral");
});

test("every campaign fixture carries the digest its approval binds", () => {
  for (const id of ["fx-refund", "fx-journey"]) {
    const { view, approval } = stage(id);
    assert.equal(view.fixture_sha256, approval?.action?.fixture_sha256, id);
  }
});

test("without the field, the diff falls back to the recorded run statuses", () => {
  const experiment = stage("fx-coverage-violation").experiment!;
  const view = (name: "baseline" | "fork") => {
    const run = experiment.runs[name]!;
    return {
      run_id: run.run_id,
      experiment_id: experiment.experiment_id,
      run: name,
      artifact: { kind: "artifact" as const, id: "artifact-1", sha256: null },
      execution: experiment.execution,
      result: run.result,
      findings: run.findings,
    };
  };
  const source = coverageOf({ coverage: null }, view("baseline"), view("fork"));
  assert.equal(source?.recorded, false);
  assert.equal(source?.coverage.complete, false);
  assert.equal(coverageOf({ coverage: null }, null, view("fork")), null);
  assert.equal(coverageOf({ coverage: experiment.coverage }, null, null)?.recorded, true);
});

test("the coverage_incomplete scenario matches the outcome and is not success", async () => {
  const source = createFixtureSource();
  const result = await source.getCampaign("fx-coverage-incomplete");
  assert.ok(result.ok);
  const view: CampaignView = result.data;
  assert.equal(view.record.status, "budget_exhausted");
  assert.equal(view.outcome, "coverage_incomplete");
  assert.equal(view.approval, null);
  assert.equal(campaignStatus(view.record.status, view.outcome).tone, "warn");
  assert.equal(readCoverage(view.experiments[0]!)?.complete, false);
  const experiment = await source.getExperiment("fx-coverage-incomplete", "experiment-1");
  assert.ok(experiment.ok);
  assert.equal(readCoverage(experiment.data)?.complete, false);
  assert.equal(experiment.data.diff.first_divergence, null);
});

test("a violation before truncation still reaches approval", async () => {
  const source = createFixtureSource();
  const view = await source.getCampaign("fx-coverage-violation");
  assert.ok(view.ok);
  assert.equal(view.data.record.status, "awaiting_approval");
  assert.equal(readCoverage(view.data.experiments[0]!)?.complete, false);
  const approval = await source.getApproval(view.data.approval!.approval_id);
  assert.ok(approval.ok);
  assert.equal(approval.data.effective_status, "pending");
  assert.equal(approval.data.request.action_digest, recorded.journey_truncated_action.action_digest);
});

test("repair_incomplete is not success with either recorded status", () => {
  for (const name of ["journey_repair_truncated", "journey_repair_idle"] as const) {
    const { repair, outcome } = recorded[name];
    assert.equal(outcome, "repair_incomplete");
    assert.equal(repair.held_by, null);
    assert.equal(repair.verified, false);
    const view = presentRepair(repair, outcome, "group-journey/1");
    assert.equal(view.tone, "warn", name);
    assert.equal(view.headline, "Repair incomplete");
  }
  assert.equal(recorded.journey_repair_truncated.campaign_status, "budget_exhausted");
  assert.equal(recorded.journey_repair_idle.campaign_status, "failed");
  assert.equal(recorded.journey_repair_idle.repair.run_status, "completed");
});

test("settings_changed is a blocked state, not a generic failure", () => {
  assert.equal(failureState(settingsChanged), "blocked");
  const summary = describeFailure(settingsChanged);
  assert.equal(summary.title, "Blocked: settings changed");
  assert.equal(summary.help, BLOCKED_HELP);
  assert.equal(summary.reason, "settings_changed");
  assert.equal(revokesAccess(settingsChanged), false, "the campaign stays on screen");
});

test("the reason decides, not the code", () => {
  const blocked: Failure = {
    kind: "service",
    status: 409,
    reason: "settings_changed",
    error: { code: "approval_invalid", message: "m", ref: null, retryable: false, side_effect: "none" },
  };
  assert.equal(failureState(blocked), "blocked");
  const other: Failure = { ...settingsChanged, reason: "awaiting_approval" } as Failure;
  assert.equal(failureState(other), "conflict");
  assert.equal(describeFailure(other).help, null);
});

test("a failed campaign with outcome settings_changed reads as blocked", () => {
  const label = campaignStatus("failed", "settings_changed");
  assert.equal(label.label, "Blocked: settings changed");
  assert.equal(label.tone, "warn");
  assert.equal(campaignStatus("failed", "repair_incomplete").tone, "fail");
});

test("a refused resume leaves the approval approved and unspent", async () => {
  const source = createFixtureSource();
  const view = await source.getCampaign("fx-resume-failed");
  assert.ok(view.ok);
  const id = view.data.approval!.approval_id;
  const approval = await source.getApproval(id);
  assert.ok(approval.ok);
  const decided = await source.decideApproval(buildSubmission(approval.data, "approve", ""), "key-1");
  assert.ok(decided.ok);
  assert.equal(decided.data.resumed, false);
  const resumed = await source.resumeCampaign("fx-resume-failed");
  assert.equal(resumed.ok, false);
  if (!resumed.ok) assert.equal(failureState(resumed.failure), "blocked");
  const after = await source.getApproval(id);
  assert.ok(after.ok);
  assert.equal(after.data.effective_status, "approved", "not consumed");
  const campaign = await source.getCampaign("fx-resume-failed");
  assert.ok(campaign.ok);
  assert.equal(campaign.data.record.status, "awaiting_approval");
  assert.equal(campaign.data.repair, null);
});

test("the blocked scenario keeps its approval unspent", async () => {
  const source = createFixtureSource();
  const view = await source.getCampaign("fx-settings-blocked");
  assert.ok(view.ok);
  assert.equal(view.data.record.status, "failed");
  assert.equal(view.data.outcome, "settings_changed");
  const approval = await source.getApproval(view.data.approval!.approval_id);
  assert.ok(approval.ok);
  assert.equal(approval.data.effective_status, "approved");
});

test("redacted text reaches the UI unchanged", async () => {
  const source = createFixtureSource();
  const view = await source.getCampaign("fx-redacted");
  assert.ok(view.ok);
  assert.match(view.data.experiments[0]!.hypothesis, /\[REDACTED\]/);
  const events = await source.listEvents("fx-redacted", 0);
  assert.ok(events.ok);
  assert.match(events.data.events.at(-1)!.summary, /\(secret-shaped text redacted\)$/);
});
