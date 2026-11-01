import assert from "node:assert/strict";
import { test } from "node:test";
import type { CampaignView, TaskSpec, WorldTraceEvent } from "../../src/api/api.generated.ts";
import { SCENARIOS } from "../../src/fixtures/scenarios.ts";
import { alignTraces } from "../../src/logic/evidence.ts";
import { alignedRowDiffers, changedMetrics, compareLedgers, summarizeAligned } from "../../src/logic/diffSummary.ts";
import { describeEventKind } from "../../src/logic/eventKinds.ts";
import {
  GUIDED_DEMO_BUDGET,
  GUIDED_DEMO_CRITERION,
  GUIDED_DEMO_OBJECTIVE,
  GUIDED_DEMO_SCENARIO,
  GUIDED_DEMO_WORLD,
  guidedDemoAvailable,
  guidedDemoInferenceNote,
  guidedDemoSpec,
} from "../../src/logic/guidedDemo.ts";
import { countTasks, summarizeProgress } from "../../src/logic/progress.ts";

const scenario = (id: string) => SCENARIOS.find((item) => item.id === id)!;
const running = scenario("fx-running").stage.view;
const finished = scenario("fx-refund").approved!.view;

test("progress counts tasks and events on a running campaign", () => {
  const progress = summarizeProgress(running, { connection: "live", cursor: 5 });
  assert.deepEqual(progress.tasks, { total: 3, succeeded: 2, active: 1, waiting: 0, failed: 0, stopped: 0 });
  assert.equal(progress.experiments, 1);
  assert.equal(progress.eventsSeen, 5);
  assert.equal(progress.eventsTotal, running.event_count);
  assert.equal(progress.terminal, false);
  assert.equal(progress.updating, true);
  assert.match(progress.note, /Updating from the service/);
});

test("a terminal record with every event shown expects no further updates", () => {
  const progress = summarizeProgress(finished, { connection: "live", cursor: finished.event_count });
  assert.equal(progress.terminal, true);
  assert.equal(progress.updating, false);
  assert.match(progress.note, /No further updates are expected/);
});

test("a terminal record with events still missing keeps updating", () => {
  const progress = summarizeProgress(finished, { connection: "live", cursor: 0 });
  assert.equal(progress.updating, true);
  assert.match(progress.note, /Updating from the service/);
});

test("while reconnecting the counts are flagged as the last ones given", () => {
  const progress = summarizeProgress(running, { connection: "reconnecting", cursor: 5 });
  assert.equal(progress.updating, true);
  assert.match(progress.note, /Reconnecting/);
});

function taskWith(status: TaskSpec["status"]): TaskSpec {
  return { ...running.tasks[0]!, task_id: `task-${status}`, status };
}

test("every task status lands in exactly one bucket", () => {
  const tasks = (["pending", "ready", "running", "awaiting_approval", "succeeded", "failed", "cancelled", "skipped"] as const).map(
    taskWith,
  );
  assert.deepEqual(countTasks(tasks), { total: 8, succeeded: 1, active: 2, waiting: 2, failed: 1, stopped: 2 });
  assert.deepEqual(countTasks([]), { total: 0, succeeded: 0, active: 0, waiting: 0, failed: 0, stopped: 0 });
});

test("every campaign event kind has a plain label", () => {
  const kinds = [
    "campaign_created",
    "campaign_status_changed",
    "task_status_changed",
    "run_started",
    "tool_event_recorded",
    "run_completed",
    "finding_recorded",
    "approval_requested",
    "approval_decided",
    "budget_reserved",
    "budget_exhausted",
    "capsule_exported",
    "replay_completed",
  ] as const;
  for (const kind of kinds) {
    const info = describeEventKind(kind);
    assert.ok(info.label.length > 0, kind);
    assert.ok(!info.label.includes("_"), kind);
  }
  assert.equal(describeEventKind("approval_requested").tone, "active");
  assert.equal(describeEventKind("budget_exhausted").tone, "warn");
  assert.equal(describeEventKind("finding_recorded").tone, "neutral", "a recorded finding can pass");
});

test("the guided demo is the synthetic refund scenario", () => {
  assert.equal(GUIDED_DEMO_WORLD, "refund-world/1");
  assert.equal(GUIDED_DEMO_SCENARIO, "default");
  assert.match(GUIDED_DEMO_OBJECTIVE, /synthetic/);
  assert.match(GUIDED_DEMO_OBJECTIVE, /2,500/);
  assert.match(GUIDED_DEMO_CRITERION, /verifier/i);
  const spec = guidedDemoSpec();
  assert.equal(spec.world_version, "refund-world/1");
  assert.deepEqual(spec.success_criteria, [GUIDED_DEMO_CRITERION]);
  assert.deepEqual(spec.budget, GUIDED_DEMO_BUDGET);
  assert.equal(GUIDED_DEMO_BUDGET.max_model_calls, 20);
  assert.equal(GUIDED_DEMO_BUDGET.max_tool_calls, 60);
  assert.ok(guidedDemoAvailable(["refund-world/1", "group-journey/1"]));
  assert.ok(!guidedDemoAvailable(["group-journey/1"]));
  assert.ok(!guidedDemoAvailable([]));
});

test("the guided demo names the service inference honestly", () => {
  assert.match(guidedDemoInferenceNote("scripted"), /not a model/);
  assert.match(guidedDemoInferenceNote("fixture"), /not a model/);
  assert.match(guidedDemoInferenceNote("disabled"), /disabled/);
  assert.match(guidedDemoInferenceNote("unrecorded"), /not recorded/);
  assert.equal(guidedDemoInferenceNote("live"), "inference live");
});

const refund = scenario("fx-refund").stage.experiment!;
const refundRows = alignTraces(refund.runs.baseline!.result, refund.runs.fork!.result);

test("the refund diff summary counts differing records", () => {
  const summary = summarizeAligned(refundRows);
  assert.ok(summary.total > 0);
  assert.ok(summary.differing > 0);
  assert.ok(summary.differingIds.includes(2), "the first divergence differs");
  assert.equal(summarizeAligned([]).total, 0);
});

function traceEvent(overrides: Record<string, unknown> = {}): WorldTraceEvent {
  return {
    event_id: 1,
    request: { tool: "get_order", arguments: {} },
    outcome: { result: { ok: true } },
    state: { ledger: [] },
    ...overrides,
  } as unknown as WorldTraceEvent;
}

test("a row differs when either record changes or a side is missing", () => {
  const same = { eventId: 1, baseline: traceEvent(), fork: traceEvent() };
  assert.equal(alignedRowDiffers(same), false);
  assert.equal(alignedRowDiffers({ ...same, fork: traceEvent({ outcome: { error: "PaymentTimeout" } }) }), true);
  assert.equal(alignedRowDiffers({ ...same, fork: traceEvent({ state: { ledger: [1] } }) }), true);
  assert.equal(alignedRowDiffers({ ...same, baseline: null }), true);
  assert.equal(alignedRowDiffers({ ...same, fork: null }), true);
});

test("changed metrics keep the recorded values with a computed delta", () => {
  const changed = changedMetrics(refund.diff);
  const refunded = changed.find((metric) => metric.name === "refunded_minor");
  assert.deepEqual(refunded, { name: "refunded_minor", baseline: 250000, fork: 500000, delta: 250000 });
  for (const metric of changed) assert.notEqual(metric.baseline, metric.fork);
});

test("the refund ledgers differ by the retried transaction", () => {
  const comparison = compareLedgers(refund.runs.baseline!.result, refund.runs.fork!.result);
  assert.deepEqual(comparison, { baselineCount: 1, forkCount: 2, baselineOnly: [], forkOnly: ["refund-2"] });
  const journey = scenario("fx-journey").stage.experiment!;
  assert.equal(compareLedgers(journey.runs.baseline!.result, journey.runs.fork!.result), null);
  assert.equal(compareLedgers(refund.runs.baseline!.result, null), null);
});

test("progress never calls a stopped campaign finished work", () => {
  const view: CampaignView = { ...finished, record: { ...finished.record, status: "failed" } };
  const progress = summarizeProgress(view, { connection: "live", cursor: view.event_count });
  assert.equal(progress.updating, false);
  assert.doesNotMatch(progress.note, /finished|complete|succeed/i);
});
