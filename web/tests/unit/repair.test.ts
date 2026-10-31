import assert from "node:assert/strict";
import { test } from "node:test";
import type { RepairView } from "../../src/api/api.generated.ts";
import { recorded } from "../../src/fixtures/recorded.generated.ts";
import { presentRepair } from "../../src/logic/repair.ts";

const JOURNEY = "group-journey/1";
const REFUND = "refund-world/1";

const base: RepairView = {
  authorized: true,
  verified: false,
  run: { id: "run-1", kind: "run", sha256: null },
  run_status: "completed",
  safety_held: true,
  task_complete: false,
  task_outcome: null,
  adapter: "journey-version-checked",
  execution: { mode: "enforced", guarded: true },
  guard_denials: [],
  held_by: null,
  lost_faults: [],
  refunded_minor: null,
};

const value = (repair: RepairView, outcome: string, label: string) =>
  presentRepair(repair, outcome, JOURNEY).fields.find((field) => field.label === label)?.value;

test("recorded repair_incomplete after a truncated run never looks like success", () => {
  const { repair, outcome, campaign_status: status } = recorded.journey_repair_truncated;
  assert.equal(status, "budget_exhausted");
  assert.equal(outcome, "repair_incomplete");
  assert.equal(repair.safety_held, true, "the trap: safety held, yet nothing was proved");
  const view = presentRepair(repair, outcome, JOURNEY);
  assert.equal(view.tone, "warn");
  assert.equal(view.headline, "Repair incomplete");
  assert.doesNotMatch(view.headline, /verified/i);
});

test("a finished run that attempted nothing is incomplete, not successful", () => {
  const view = presentRepair({ ...base, task_complete: false }, "repair_incomplete", JOURNEY);
  assert.equal(view.tone, "warn");
  assert.equal(view.headline, "Repair incomplete");
});

test("recorded verified repairs are success and name what held", () => {
  const adapter = recorded.journey_repair_held_by_adapter;
  const boundary = recorded.journey_repair_held_by_boundary;
  assert.equal(presentRepair(adapter.repair, adapter.outcome, JOURNEY).tone, "ok");
  assert.equal(value(adapter.repair, adapter.outcome, "held_by"), "adapter");
  assert.equal(value(boundary.repair, boundary.outcome, "held_by"), "guarded_boundary");
  assert.deepEqual(boundary.repair.guard_denials, [{ error: "PermissionWithdrawnDenied", event_id: 18 }]);
});

test("verified is not enough: the outcome must agree", () => {
  const repair = { ...base, verified: true, task_complete: true };
  for (const outcome of ["repair_incomplete", "repair_did_not_hold", "completed", null]) {
    const view = presentRepair(repair, outcome, JOURNEY);
    assert.equal(view.tone, "fail", String(outcome));
    assert.equal(view.headline, "Inconsistent repair record");
  }
});

test("a verified repair over an unfinished run is not success", () => {
  const view = presentRepair({ ...base, verified: true, run_status: "budget_exhausted" }, "repair_verified", JOURNEY);
  assert.equal(view.tone, "fail");
});

test("verified with an unfinished task says so", () => {
  const view = presentRepair({ ...base, verified: true }, "repair_verified", JOURNEY);
  assert.equal(view.tone, "warn");
  assert.equal(view.headline, "Repair verified, task not complete");
});

test("repair_did_not_hold is a failure", () => {
  const view = presentRepair({ ...base, safety_held: false }, "repair_did_not_hold", JOURNEY);
  assert.equal(view.tone, "fail");
});

test("an authorized repair with no result yet shows nothing as recorded", () => {
  const pending = { ...base, run: null, run_status: null, safety_held: null, task_complete: null, lost_faults: null };
  const view = presentRepair(pending, null, JOURNEY);
  assert.equal(view.tone, "active");
  assert.deepEqual(
    view.fields.filter((field) => !field.recorded).map((field) => field.label),
    ["run_status", "safety_held", "task_complete", "lost_faults", "held_by"],
  );
});

test("held_by null is explained, and differs by world", () => {
  const journey = presentRepair(base, "repair_incomplete", JOURNEY).fields.at(-1);
  const refund = presentRepair(recorded.refund_repair.repair, "repair_verified", REFUND).fields.at(-1);
  assert.equal(journey?.recorded, false);
  assert.match(journey?.value ?? "", /only a verified repair/);
  assert.equal(refund?.recorded, false);
  assert.match(refund?.value ?? "", /not recorded for this world/);
});

test("recorded: a repair that missed a fault never reads as success, even with safety and task true", () => {
  const { repair, outcome, campaign_status: status } = recorded.journey_repair_lost_fault;
  assert.equal(status, "failed");
  assert.equal(outcome, "repair_incomplete");
  assert.equal(repair.safety_held, true, "the trap: everything else looks fine");
  assert.equal(repair.task_complete, true);
  assert.deepEqual(repair.lost_faults, ["sharing_revoked_while_queued"]);
  const view = presentRepair(repair, outcome, JOURNEY);
  assert.equal(view.tone, "warn");
  assert.match(view.detail, /never met sharing_revoked_while_queued/);
  assert.equal(value(repair, outcome, "lost_faults"), "sharing_revoked_while_queued");
});

test("a lost fault outweighs a record that claims verified", () => {
  const claimed: RepairView = { ...base, verified: true, task_complete: true, lost_faults: ["a_fault"] };
  const view = presentRepair(claimed, "repair_verified", JOURNEY);
  assert.equal(view.tone, "fail");
  assert.equal(view.headline, "Inconsistent repair record");
});

test("no lost faults reads as none, and an older record as not recorded", () => {
  assert.equal(value(base, "repair_incomplete", "lost_faults"), "none");
  assert.equal(value({ ...base, lost_faults: null }, "repair_incomplete", "lost_faults"), "not recorded yet");
});

test("recorded values appear verbatim", () => {
  const { repair, outcome } = recorded.journey_repair_truncated;
  assert.equal(value(repair, outcome, "run_status"), "budget_exhausted");
  assert.equal(value(repair, outcome, "safety_held"), "true");
  assert.equal(value(repair, outcome, "task_complete"), "false");
  assert.equal(value(repair, outcome, "verified"), "false");
});
