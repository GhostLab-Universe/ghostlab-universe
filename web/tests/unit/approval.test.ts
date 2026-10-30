import assert from "node:assert/strict";
import { test } from "node:test";
import type { ApprovalView, SessionInfo } from "../../src/api/api.generated.ts";
import { createFixtureSource } from "../../src/data/fixtureSource.ts";
import { SCENARIOS } from "../../src/fixtures/scenarios.ts";
import { actionFacts, buildSubmission, decisionGate, IdempotencyKeys } from "../../src/logic/approval.ts";

const refund = SCENARIOS.find((scenario) => scenario.id === "fx-refund")!.stage.approval!;
const journey = SCENARIOS.find((scenario) => scenario.id === "fx-journey")!.stage.approval!;

const session: SessionInfo = {
  schema_version: 1,
  principal: { kind: "local_dev", principal_id: "local-dev-user" },
  project_id: "project-local",
  auth_mode: "local_dev",
  production_authenticated: false,
  can_approve: true,
  inference: "disabled",
  runnable_worlds: ["refund-world/1"],
};

test("the submission holds only the contract's fields, bound to the request", () => {
  const submission = buildSubmission(refund, "approve", "  checked the diff  ");
  assert.deepEqual(Object.keys(submission).sort(), ["action_digest", "approval_id", "decision", "reason", "schema_version"]);
  assert.equal(submission.action_digest, refund.request.action_digest);
  assert.equal(submission.approval_id, refund.request.approval_id);
  assert.equal(submission.reason, "checked the diff");
  assert.equal(buildSubmission(refund, "reject", "   ").reason, null);
});

test("the journey action shows the recorded execution mode and fixture digest", () => {
  const facts = actionFacts(journey.action);
  assert.equal(facts.mode, "enforced");
  assert.equal(facts.guarded, true);
  assert.match(facts.fixtureSha256 ?? "", /^[0-9a-f]{64}$/);
});

test("a missing action field stays null, not a default", () => {
  assert.deepEqual(actionFacts(null), { adapter: null, mode: null, guarded: null, fixtureSha256: null });
  const refundFacts = actionFacts(refund.action);
  assert.equal(refundFacts.mode, null);
  assert.match(refundFacts.fixtureSha256 ?? "", /^[0-9a-f]{64}$/);
});

test("the gate follows the service's effective status, not the stored one", () => {
  const expired: ApprovalView = { ...refund, effective_status: "expired" };
  assert.equal(expired.request.status, "pending");
  assert.equal(decisionGate(expired, session).open, false);
  assert.equal(decisionGate(refund, session).open, true);
  assert.equal(decisionGate(refund, { ...session, can_approve: false }).open, false);
});

test("a retry of the same body reuses its key; any change is a new action", () => {
  let minted = 0;
  const keys = new IdempotencyKeys(() => `key-${(minted += 1)}`);
  const approve = buildSubmission(refund, "approve", "");
  assert.equal(keys.keyFor(approve), "key-1");
  assert.equal(keys.keyFor({ ...approve }), "key-1");
  assert.equal(keys.keyFor(buildSubmission(refund, "reject", "")), "key-2");
  assert.equal(keys.keyFor(buildSubmission(refund, "approve", "a reason")), "key-3");
});

test("a default key is a valid Idempotency-Key", () => {
  const key = new IdempotencyKeys().keyFor(buildSubmission(refund, "approve", ""));
  assert.match(key, /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/);
});

test("a denied decision changes nothing on the service", async () => {
  const source = createFixtureSource();
  const id = "fx-approval-denied";
  const view = await source.getCampaign(id);
  assert.ok(view.ok);
  const approvalId = view.data.approval!.approval_id;
  const approval = await source.getApproval(approvalId);
  assert.ok(approval.ok);
  const result = await source.decideApproval(buildSubmission(approval.data, "approve", ""), "key-1");
  assert.equal(result.ok, false);
  const after = await source.getApproval(approvalId);
  assert.ok(after.ok);
  assert.equal(after.data.effective_status, "pending");
  assert.equal(after.data.decision, null);
  const campaign = await source.getCampaign(id);
  assert.ok(campaign.ok);
  assert.equal(campaign.data.record.status, "awaiting_approval");
});

test("a digest that does not match is refused", async () => {
  const source = createFixtureSource();
  const approval = await source.getApproval(refund.request.approval_id);
  assert.ok(approval.ok);
  const forged = { ...buildSubmission(approval.data, "approve", ""), action_digest: "0".repeat(64) };
  const result = await source.decideApproval(forged, "key-1");
  assert.equal(result.ok, false);
  if (!result.ok && result.failure.kind === "service") assert.equal(result.failure.reason, "digest_mismatch");
});

test("a recorded decision whose resume failed still reports the failure", async () => {
  const source = createFixtureSource();
  const view = await source.getCampaign("fx-resume-failed");
  assert.ok(view.ok);
  const approval = await source.getApproval(view.data.approval!.approval_id);
  assert.ok(approval.ok);
  const result = await source.decideApproval(buildSubmission(approval.data, "approve", ""), "key-1");
  assert.ok(result.ok);
  assert.equal(result.data.resumed, false);
  assert.equal(result.data.resume_error?.reason, "settings_changed");
});
