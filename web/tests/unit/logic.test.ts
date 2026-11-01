import assert from "node:assert/strict";
import { test } from "node:test";
import type { TaskSpec, WorldInvariantFinding, WorldRealityDiff } from "../../src/api/api.generated.ts";
import { recorded } from "../../src/fixtures/recorded.generated.ts";
import { SCENARIOS } from "../../src/fixtures/scenarios.ts";
import {
  alignTraces,
  findingsForEvent,
  metricRows,
  parseEventRef,
  refundLedger,
  violatedInvariants,
  violatingEventIds,
} from "../../src/logic/evidence.ts";
import { describeFailure, failureState, revokesAccess } from "../../src/logic/failure.ts";
import { formatMinor } from "../../src/logic/format.ts";
import { campaignHref, parseRoute } from "../../src/logic/routes.ts";
import { differences } from "../../src/logic/stateDiff.ts";
import { campaignStatus } from "../../src/logic/status.ts";
import { orderTasks } from "../../src/logic/timeline.ts";

const experiment = (id: string) => SCENARIOS.find((scenario) => scenario.id === id)!.stage.experiment!;
const journey = experiment("fx-journey");
const refund = experiment("fx-refund");

test("journey: the first divergence and the violating event are different events", () => {
  assert.equal(journey.diff.first_divergence?.event_id, 10);
  assert.deepEqual(violatingEventIds(journey.findings), [12]);
});

test("journey: only state differs at the first divergence", () => {
  const divergence = journey.diff.first_divergence!;
  assert.deepEqual(differences(divergence.baseline?.request, divergence.faulted?.request), []);
  assert.deepEqual(differences(divergence.baseline?.outcome, divergence.faulted?.outcome), []);
  const paths = differences(divergence.baseline?.state, divergence.faulted?.state).map((item) => item.path);
  assert.deepEqual(paths, ["permission_timeline[3]", "sharing.participant-c"]);
});

test("refund: the outcome differs at the first divergence and the state does not", () => {
  const divergence = refund.diff.first_divergence!;
  assert.equal(divergence.event_id, 2);
  assert.deepEqual(differences(divergence.baseline?.state, divergence.faulted?.state), []);
  assert.ok(differences(divergence.baseline?.outcome, divergence.faulted?.outcome).length > 0);
});

test("the published diff's baseline event is the recorded baseline run's event", () => {
  const [row] = alignTraces(recorded.journey_baseline_run, null).filter((item) => item.eventId === 10);
  assert.deepEqual(row?.baseline, journey.diff.first_divergence?.baseline);
});

test("every violated invariant is matched to a verifier finding, or reported as missing", () => {
  const rows = violatedInvariants(refund.diff, refund.findings);
  assert.deepEqual(rows.map((row) => [row.invariant, row.findings.length]), [
    ["total_refund_within_payment", 1],
    ["no_duplicate_full_refund", 1],
  ]);
  const none = violatedInvariants(refund.diff, []);
  assert.deepEqual(none.map((row) => row.findings.length), [0, 0]);
});

test("a passed finding does not mark its event as violating", () => {
  const passed = recorded.refund_repair.findings;
  assert.deepEqual(violatingEventIds(passed), []);
  assert.equal(findingsForEvent(passed, 2).length, 1);
});

test("traces of different lengths align without inventing events", () => {
  const rows = alignTraces(refund.runs.baseline!.result, refund.runs.fork!.result);
  assert.deepEqual(rows.map((row) => [row.eventId, row.baseline !== null, row.fork !== null]), [
    [1, true, true],
    [2, true, true],
    [3, false, true],
  ]);
});

test("ledger totals are the recorded ones, not a sum", () => {
  const fork = refund.runs.fork!.result;
  const claimed = { ...fork, final_state: { ...fork.final_state, total_refunded_minor: 123 } };
  const ledger = refundLedger(claimed);
  assert.equal(ledger?.totalRefundedMinor, 123);
  assert.equal(ledger?.rows.length, 2);
  assert.equal(ledger?.currency, "INR");
  assert.equal(refundLedger(journey.runs.fork!.result), null);
});

test("metric rows show recorded values and leave a missing side empty", () => {
  const diff = { ...refund.diff, baseline_metrics: {}, faulted_metrics: { refunded_minor: 500000 } } as WorldRealityDiff;
  assert.deepEqual(metricRows(diff), [{ name: "refunded_minor", baseline: null, fork: 500000 }]);
});

test("recorded money reads as money", () => {
  assert.equal(formatMinor(250000, "INR"), "INR 2,500.00");
  assert.equal(formatMinor(500000, "INR"), "INR 5,000.00");
  assert.equal(formatMinor(250, "JPY"), "JPY 250");
});

test("event refs parse, and anything else stays plain text", () => {
  assert.deepEqual(parseEventRef("run-faulted-1:2"), { runId: "run-faulted-1", eventId: 2 });
  assert.equal(parseEventRef("finding-1"), null);
});

test("'completed' never reads as success", () => {
  assert.equal(campaignStatus("completed").tone, "neutral");
  assert.doesNotMatch(campaignStatus("completed").label, /^completed$/i);
});

test("tasks order by dependency and unknown dependencies stay visible", () => {
  const task = (task_id: string, depends_on: string[]): TaskSpec => ({
    ...SCENARIOS[0]!.stage.view.tasks[0]!,
    task_id,
    depends_on,
  });
  const ordered = orderTasks([task("c", ["b"]), task("a", []), task("b", ["a", "missing"])]);
  assert.deepEqual(ordered.map((item) => item.task.task_id), ["a", "b", "c"]);
  assert.deepEqual(ordered[1]?.unknownDependencies, ["missing"]);
});

test("a dependency cycle still lists every task once", () => {
  const task = (task_id: string, depends_on: string[]): TaskSpec => ({
    ...SCENARIOS[0]!.stage.view.tasks[0]!,
    task_id,
    depends_on,
  });
  assert.equal(orderTasks([task("a", ["b"]), task("b", ["a"])]).length, 2);
});

test("routes round-trip through the hash", () => {
  const href = campaignHref("fx refund", { view: "evidence", experimentId: "experiment-1", run: "fork", eventId: 12 });
  assert.deepEqual(parseRoute(href), {
    page: "campaign",
    campaignId: "fx refund",
    view: "evidence",
    experimentId: "experiment-1",
    run: "fork",
    eventId: 12,
  });
});

test("an unknown route falls back safely", () => {
  assert.deepEqual(parseRoute("#/nonsense"), { page: "home" });
  assert.deepEqual(parseRoute("#/campaign/%E0%A4%A"), { page: "home" });
  const route = parseRoute("#/campaign/c/admin?run=root&event=-1");
  assert.equal(route.page === "campaign" && route.view, "overview");
  assert.equal(route.page === "campaign" && route.run, null);
  assert.equal(route.page === "campaign" && route.eventId, null);
});

test("failures map to the required states", () => {
  const service = (code: "forbidden" | "unauthenticated" | "validation_failed" | "cancelled" | "not_found" | "internal") => ({
    kind: "service" as const,
    status: 400,
    reason: null,
    error: { code, message: "m", ref: null, retryable: false, side_effect: "unknown" as const },
  });
  assert.equal(failureState(service("forbidden")), "denied");
  assert.equal(failureState(service("unauthenticated")), "denied");
  assert.equal(failureState(service("validation_failed")), "validation_error");
  assert.equal(failureState(service("cancelled")), "cancelled");
  assert.equal(failureState(service("internal")), "failed");
  assert.equal(failureState({ kind: "transport", message: "m" }), "reconnecting");
  assert.equal(failureState({ kind: "invalid_response", operation: "x", message: "m" }), "failed");
  assert.equal(revokesAccess(service("not_found")), true);
  assert.equal(revokesAccess(service("internal")), false);
  assert.equal(describeFailure(service("internal")).sideEffect, "unknown");
});

test("findings keep the verifier's own fields", () => {
  const [finding] = journey.findings.filter((item: WorldInvariantFinding) => item.verdict === "failed");
  assert.equal(finding?.invariant, "delivered_messages_permitted");
  assert.equal(finding?.verifier_version, "journey-verifier-1");
});
