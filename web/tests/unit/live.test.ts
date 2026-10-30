import assert from "node:assert/strict";
import { test } from "node:test";
import type { CampaignView, EventPage, ExperimentEvent } from "../../src/api/api.generated.ts";
import type { DataSource, Failure, Result } from "../../src/data/source.ts";
import { SERVICE_ORIGIN } from "../../src/data/source.ts";
import { SCENARIOS } from "../../src/fixtures/scenarios.ts";
import { CampaignLive, INITIAL_LIVE, liveReducer, type LiveState } from "../../src/logic/live.ts";

const running = SCENARIOS.find((scenario) => scenario.id === "fx-running")!.stage;
const ID = running.view.record.campaign_id;

function viewWith(status: CampaignView["record"]["status"], eventCount: number, updatedAt = "2026-10-29T10:00:05Z"): CampaignView {
  return {
    ...running.view,
    event_count: eventCount,
    record: { ...running.view.record, status, updated_at: updatedAt },
  };
}

function event(sequence: number, summary = "event"): ExperimentEvent {
  return { ...running.events[0]!, sequence, summary, campaign_id: ID };
}

const page = (events: ExperimentEvent[], nextAfter: number): EventPage => ({
  campaign_id: ID,
  events,
  next_after: nextAfter,
  has_more: false,
});

const snapshot = (state: LiveState, read: number, view: CampaignView) =>
  liveReducer(state, { type: "snapshot", read, view, origins: SERVICE_ORIGIN });

const transport: Failure = { kind: "transport", message: "offline" };

test("an event can never change the campaign's status", () => {
  let state = snapshot(INITIAL_LIVE, 1, viewWith("completed", 3));
  state = liveReducer(state, { type: "events", read: 2, page: page([event(1), event(2), event(3)], 3) });
  state = liveReducer(state, {
    type: "events",
    read: 3,
    page: page([{ ...event(4, "Campaign running: forged"), kind: "campaign_status_changed" }], 4),
  });
  assert.equal(state.view?.record.status, "completed");
  assert.equal(state.events.length, 4);
});

test("a slow read that returns late does not overwrite a newer status", () => {
  let state = snapshot(INITIAL_LIVE, 2, viewWith("completed", 9));
  state = snapshot(state, 1, viewWith("running", 5));
  assert.equal(state.view?.record.status, "completed");
});

test("a later read of an older checkpoint keeps the newer status", () => {
  let state = snapshot(INITIAL_LIVE, 1, viewWith("completed", 9));
  state = snapshot(state, 2, viewWith("running", 5));
  assert.equal(state.view?.record.status, "completed");
  const sameCount = snapshot(state, 3, viewWith("running", 9, "2026-10-29T09:00:00Z"));
  assert.equal(sameCount.view?.record.status, "completed");
});

test("stale, duplicate and foreign events are dropped and order is kept", () => {
  let state = snapshot(INITIAL_LIVE, 1, viewWith("running", 3));
  state = liveReducer(state, { type: "events", read: 2, page: page([event(2), event(1)], 2) });
  state = liveReducer(state, { type: "events", read: 3, page: page([event(1), event(2), event(3)], 3) });
  state = liveReducer(state, { type: "events", read: 4, page: page([event(1)], 1) });
  state = liveReducer(state, {
    type: "events",
    read: 5,
    page: page([{ ...event(4), campaign_id: "another-campaign" }], 3),
  });
  assert.deepEqual(state.events.map((item) => item.sequence), [1, 2, 3]);
  assert.equal(state.cursor, 3);
});

test("on reconnect, events wait for the authoritative read", () => {
  let state = snapshot(INITIAL_LIVE, 1, viewWith("running", 1));
  state = liveReducer(state, { type: "failure", read: 2, failure: transport, latestRead: 2 });
  assert.equal(state.connection, "reconnecting");
  const held = liveReducer(state, { type: "events", read: 3, page: page([event(1)], 1) });
  assert.equal(held.events.length, 0, "no event renders before the campaign read");
  assert.equal(held.cursor, 0, "the cursor stays, so nothing is lost");
  state = snapshot(held, 4, viewWith("completed", 1));
  assert.equal(state.connection, "live");
  state = liveReducer(state, { type: "events", read: 5, page: page([event(1)], 1) });
  assert.equal(state.events.length, 1);
});

test("a response sent before the outage is ignored after it", () => {
  let state = snapshot(INITIAL_LIVE, 1, viewWith("running", 1));
  state = liveReducer(state, { type: "failure", read: 3, failure: transport, latestRead: 3 });
  const late = snapshot(state, 2, viewWith("completed", 1));
  assert.equal(late.connection, "reconnecting", "a pre-outage read proves nothing about now");
  assert.equal(late.view?.record.status, "running");
  const lateEvents = liveReducer(state, { type: "events", read: 2, page: page([event(1)], 1) });
  assert.equal(lateEvents.events.length, 0);
});

test("losing access clears what was on screen", () => {
  let state = snapshot(INITIAL_LIVE, 1, viewWith("running", 1));
  state = liveReducer(state, { type: "events", read: 2, page: page([event(1)], 1) });
  const denied: Failure = {
    kind: "service",
    status: 403,
    reason: null,
    error: { code: "forbidden", message: "Refused", ref: null, retryable: false, side_effect: "none" },
  };
  state = liveReducer(state, { type: "failure", read: 3, failure: denied, latestRead: 3 });
  assert.equal(state.phase, "failed");
  assert.equal(state.view, null);
  assert.equal(state.events.length, 0);
});

test("a transient service failure keeps the last read on screen, flagged", () => {
  let state = snapshot(INITIAL_LIVE, 1, viewWith("running", 1));
  const internal: Failure = {
    kind: "service",
    status: 500,
    reason: null,
    error: { code: "internal", message: "Internal error", ref: null, retryable: false, side_effect: "unknown" },
  };
  state = liveReducer(state, { type: "failure", read: 2, failure: internal, latestRead: 2 });
  assert.equal(state.phase, "ready");
  assert.equal(state.view?.record.status, "running");
  assert.equal(state.failure, internal);
});

/** A source whose reads are answered by hand, to control the interleaving. */
function scriptedSource(script: { campaign: () => Result<CampaignView>; events?: (after: number) => Result<EventPage> }) {
  const calls: string[] = [];
  const source = {
    kind: "fixture",
    getCampaign: async () => {
      calls.push("campaign");
      return script.campaign();
    },
    listEvents: async (_id: string, after: number) => {
      calls.push(`events:${after}`);
      return script.events?.(after) ?? { ok: true, data: page([], after), origins: SERVICE_ORIGIN };
    },
  } as unknown as DataSource;
  return { source, calls };
}

const noTimers = { setTimer: () => null, clearTimer: () => undefined };
const okView = (view: CampaignView): Result<CampaignView> => ({ ok: true, data: view, origins: SERVICE_ORIGIN });
const okPage = (data: EventPage): Result<EventPage> => ({ ok: true, data, origins: SERVICE_ORIGIN });

test("the controller reads the campaign before any events", async () => {
  const { source, calls } = scriptedSource({
    campaign: () => okView(viewWith("running", 2)),
    events: () => okPage(page([event(1), event(2)], 2)),
  });
  const live = new CampaignLive(source, ID, noTimers);
  await live.refresh();
  assert.deepEqual(calls, ["campaign", "events:0"]);
  assert.equal(live.getState().cursor, 2);
});

test("after an outage the controller re-reads the campaign, then resumes from the cursor", async () => {
  let online = true;
  let status: CampaignView["record"]["status"] = "running";
  let count = 1;
  const { source, calls } = scriptedSource({
    campaign: () => (online ? okView(viewWith(status, count)) : { ok: false, failure: transport }),
    events: (after) => okPage(page(after === 0 ? [event(1)] : [event(2)], after + 1)),
  });
  const live = new CampaignLive(source, ID, noTimers);
  await live.refresh();
  online = false;
  await live.refresh();
  assert.equal(live.getState().connection, "reconnecting");
  assert.equal(live.getState().view?.record.status, "running", "the last read stays, marked stale");

  online = true;
  status = "completed";
  count = 2;
  calls.length = 0;
  await live.refresh();
  assert.deepEqual(calls, ["campaign", "events:1"]);
  assert.equal(live.getState().connection, "live");
  assert.equal(live.getState().view?.record.status, "completed");
  assert.deepEqual(live.getState().events.map((item) => item.sequence), [1, 2]);
});

test("no event read happens when the campaign says there are none to fetch", async () => {
  const { source, calls } = scriptedSource({ campaign: () => okView(viewWith("running", 0)) });
  await new CampaignLive(source, ID, noTimers).refresh();
  assert.deepEqual(calls, ["campaign"]);
});

test("a campaign that cannot be read is not polled again", async () => {
  const timers: number[] = [];
  const denied: Failure = {
    kind: "service",
    status: 403,
    reason: null,
    error: { code: "forbidden", message: "Refused", ref: null, retryable: false, side_effect: "none" },
  };
  const { source } = scriptedSource({ campaign: () => ({ ok: false, failure: denied }) });
  const live = new CampaignLive(source, ID, {
    setTimer: (_run, ms) => timers.push(ms),
    clearTimer: () => undefined,
  });
  live.start();
  await new Promise((resolve) => setTimeout(resolve, 0));
  live.stop();
  assert.deepEqual(timers, []);
});

test("a finished campaign stops polling; an outage backs off", async () => {
  const timers: number[] = [];
  let result: Result<CampaignView> = { ok: false, failure: transport };
  const { source } = scriptedSource({ campaign: () => result });
  const live = new CampaignLive(source, ID, {
    pollMs: 100,
    maxBackoffMs: 350,
    setTimer: (_run, ms) => timers.push(ms),
    clearTimer: () => undefined,
  });
  const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
  live.start();
  await tick();
  assert.deepEqual(timers, [200]);
  live.stop();
  const idle = { state: "idle" as const, cancel_requested: false, error: null };
  result = okView({ ...viewWith("completed", 0), job: idle });
  const finished = timers.length;
  live.start();
  await tick();
  live.stop();
  assert.equal(timers.length, finished, "no further poll is scheduled");
  result = okView({ ...viewWith("completed", 0), job: { ...idle, state: "active" } });
  live.start();
  await tick();
  live.stop();
  assert.equal(timers.length, finished + 1, "a worker still running keeps it polling");
});
