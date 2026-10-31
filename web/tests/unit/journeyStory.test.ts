import assert from "node:assert/strict";
import { test } from "node:test";
import type { WorldTraceEvent } from "../../src/api/api.generated.ts";
import { recorded } from "../../src/fixtures/recorded.generated.ts";
import { SCENARIOS } from "../../src/fixtures/scenarios.ts";
import { looksExpired, stoppedNote } from "../../src/logic/approval.ts";
import { checkSentence, deliveryAt, divergenceCause, leaksIn, leakSentence, sharingLostAt } from "../../src/logic/journeyStory.ts";
import { presentReplay } from "../../src/logic/replay.ts";

const experiment = (id: string) => SCENARIOS.find((scenario) => scenario.id === id)!.stage.experiment!;

test("the default journey names who leaked, from recorded evidence", () => {
  const journey = experiment("fx-journey");
  const [leak, ...rest] = leaksIn(journey.runs.fork!.result, journey.findings);
  assert.deepEqual(rest, []);
  assert.equal(leakSentence(leak!), "participant-c lost sharing at event 10; msg-3 was delivered to them at event 12.");
  assert.equal(leak?.recipient, "participant-c");
  assert.equal(leak?.messageId, "msg-3");
  assert.equal(leak?.sharing, false);
});

test("event 10 is named as the permission change, not an unrelated delivery", () => {
  const cause = divergenceCause(experiment("fx-journey").diff.first_divergence!);
  assert.equal(
    cause,
    "The worlds separate because participant-c lost sharing at event 10. That event's delivery of msg-1 went to traveller-a, who still had sharing, and is not the violation.",
  );
});

test("the race reads as clearly: the careful adapter's last check, then the leak", () => {
  const race = recorded.journey_race;
  assert.equal(race.fork.adapter_id, "journey-version-checked");
  const [leak] = leaksIn(race.fork, race.findings);
  assert.equal(leakSentence(leak!), "participant-c lost sharing at event 18; msg-3 was delivered to them at event 18.");
  assert.equal(leak?.lastReadAllowedAt, 17);
  assert.match(checkSentence(leak!) ?? "", /allowed at event 17/);
  assert.equal(
    divergenceCause(race.diff.first_divergence!),
    "The worlds separate because participant-c lost sharing at event 18. The same event delivered msg-3 to them.",
  );
});

test("the story changes with the evidence: it is never written by hand", () => {
  const race = recorded.journey_race;
  const trace = race.fork.trace.map((event) => JSON.parse(JSON.stringify(event).replaceAll("participant-c", "someone-else")) as WorldTraceEvent);
  const [leak] = leaksIn({ ...race.fork, trace }, race.findings);
  assert.equal(leak?.recipient, "someone-else");
  assert.match(leakSentence(leak!), /^someone-else lost sharing/);
});

test("no leak is claimed without a verifier finding", () => {
  assert.deepEqual(leaksIn(recorded.journey_race.fork, []), []);
  const passed = recorded.journey_race.findings.map((finding) => ({ ...finding, verdict: "passed" as const }));
  assert.deepEqual(leaksIn(recorded.journey_race.fork, passed), []);
});

test("no leak is claimed when the recorded state shows sharing intact", () => {
  const race = recorded.journey_race;
  const trace = race.fork.trace.map((event) => ({
    ...event,
    state: { ...event.state, sharing: { "participant-c": true } },
  }));
  assert.deepEqual(leaksIn({ ...race.fork, trace }, race.findings), []);
});

test("the guard's refusal of the same delivery is recorded as denied", () => {
  const { run } = recorded.journey_repair_held_by_boundary;
  const event = run.trace.find((item) => item.event_id === 18)!;
  const denial = deliveryAt(event);
  assert.equal(denial?.messageId, "msg-3");
  assert.equal(denial?.recipient, "participant-c");
  assert.equal(denial?.denied, "PermissionWithdrawnDenied");
  assert.deepEqual(leaksIn(run, recorded.journey_repair_held_by_boundary.findings), []);
});

test("an event names only the sharing that ended there", () => {
  const trace = experiment("fx-journey").runs.fork!.result.trace;
  assert.deepEqual(sharingLostAt(trace.find((event) => event.event_id === 10)!), ["participant-c"]);
  assert.deepEqual(sharingLostAt(trace.find((event) => event.event_id === 12)!), []);
});

test("refund evidence has no journey story", () => {
  const refund = experiment("fx-refund");
  assert.deepEqual(leaksIn(refund.runs.fork!.result, refund.findings), []);
  assert.equal(divergenceCause(refund.diff.first_divergence!), null);
});

test("the stopped warning appears only while the approval can still matter", () => {
  assert.match(stoppedNote("pending", true, false) ?? "", /stays unspent/);
  assert.match(stoppedNote("approved", true, false) ?? "", /never used/);
  assert.equal(stoppedNote("consumed", true, true), null, "the key demo moment");
  assert.equal(stoppedNote("consumed", true, false), null);
  assert.equal(stoppedNote("rejected", true, false), null);
  assert.equal(stoppedNote("approved", true, true), null);
  assert.equal(stoppedNote("expired", true, false), null);
  assert.equal(stoppedNote("pending", false, false), null);
});

test("a replay of a repair that missed a fault is not read as clean", () => {
  const clean = { ...recorded.refund_replay, checks: { a: true } };
  assert.equal(presentReplay(clean).tone, "neutral");
  const missed = presentReplay(clean, ["sharing_revoked_while_queued"]);
  assert.equal(missed.tone, "warn");
  assert.match(missed.detail, /never met sharing_revoked_while_queued/);
});

test("expiry wording follows the recorded time", () => {
  assert.equal(looksExpired("2026-01-01T00:00:00Z", Date.parse("2026-06-01T00:00:00Z")), true);
  assert.equal(looksExpired("2099-01-01T00:00:00Z", Date.parse("2026-06-01T00:00:00Z")), false);
  assert.equal(looksExpired(undefined, 0), false);
});
