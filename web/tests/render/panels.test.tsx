import assert from "node:assert/strict";
import { test } from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import type { ReactNode } from "react";
import type { ApprovalView, ExtInspection, RunView, SessionInfo } from "../../src/api/api.generated.ts";
import { createFixtureSource } from "../../src/data/fixtureSource.ts";
import type { DataSource, Failure, Origin } from "../../src/data/source.ts";
import { recorded } from "../../src/fixtures/recorded.generated.ts";
import { SCENARIOS } from "../../src/fixtures/scenarios.ts";
import { App, TestDataBanner } from "../../src/ui/App.tsx";
import { FailureNotice, OriginNote, Notice } from "../../src/ui/components.tsx";
import { ApprovalDetail, DecisionPanel } from "../../src/views/ApprovalPage.tsx";
import { ProgressPanel, RepairResult } from "../../src/views/CampaignPage.tsx";
import { GuidedDemo } from "../../src/views/HomePage.tsx";
import { EnforcementPanel, ReplayPanel } from "../../src/views/CapsulePage.tsx";
import { DiffPanel } from "../../src/views/DiffPage.tsx";
import { EnforcementOutcome } from "../../src/views/ExtensionsPage.tsx";
import { ApplicationEvents, FindingList, RunEvidence } from "../../src/views/EvidencePage.tsx";

const html = (node: ReactNode) => renderToStaticMarkup(node);
const text = (markup: string) => markup.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
const scenario = (id: string) => SCENARIOS.find((item) => item.id === id)!;

const refund = scenario("fx-refund").stage.experiment!;
const journey = scenario("fx-journey").stage.experiment!;
const repairedOrigins: readonly Origin[] = [{ kind: "recorded_core_output", source: "test", note: null }];

function toView(experiment: typeof refund) {
  return {
    experiment_id: experiment.experiment_id,
    iteration: 1,
    proposal: experiment.proposal,
    execution: experiment.execution,
    diff: experiment.diff,
    findings: experiment.findings,
    coverage: experiment.coverage,
    runs: Object.keys(experiment.runs) as ["baseline", "fork"],
    inference: "fixture" as const,
  };
}

function runView(experiment: typeof refund, name: "baseline" | "fork"): RunView {
  const run = experiment.runs[name]!;
  return {
    run_id: run.run_id,
    experiment_id: experiment.experiment_id,
    run: name,
    artifact: { kind: "artifact", id: "artifact-1", sha256: null },
    execution: experiment.execution,
    result: run.result,
    findings: run.findings,
    inference: "fixture" as const,
  };
}

const diffPanel = (experiment: typeof refund) =>
  html(
    <DiffPanel
      campaignId="fx"
      experiment={toView(experiment)}
      origins={experiment.origins}
      baseline={runView(experiment, "baseline")}
      fork={runView(experiment, "fork")}
      runOrigins={experiment.origins}
    />,
  );

function section(markup: string, testId: string): string {
  const start = markup.indexOf(`data-testid="${testId}"`);
  assert.notEqual(start, -1, testId);
  return markup.slice(start, start + 900);
}

test("the journey diff shows first divergence and violation as separate events", () => {
  const markup = diffPanel(journey);
  assert.match(text(section(markup, "first-divergence")), /First divergence Event 10/);
  const violation = text(section(markup, "violating-events"));
  assert.match(violation, /Violation evidence Event 12/);
  assert.match(violation, /different event from the first divergence/);
});

test("the journey diff shows metrics, violated invariants and evidence event ids", () => {
  const page = text(diffPanel(journey));
  assert.match(page, /messages_delivered_without_permission 0 1/);
  assert.match(page, /violated delivered_messages_permitted/);
  assert.match(page, /Evidence events 12/);
  assert.match(page, /journey-run-1-delivered_messages_permitted/);
});

test("the journey diff points at the state that differs, since request and outcome match", () => {
  const table = text(section(diffPanel(journey), "difference-table"));
  assert.match(table, /sharing\.participant-c true false/);
  assert.match(table, /permission_timeline\[3\] absent/);
});

test("the refund diff shows recorded ledger totals side by side", () => {
  const page = text(diffPanel(refund));
  assert.match(page, /refunded_minor 250000 INR 2,500\.00 500000 INR 5,000\.00/);
  assert.match(page, /Total refunded 250000 minor units/);
  assert.match(page, /Total refunded 500000 minor units/);
  assert.match(page, /refund-1 250000/);
  assert.match(page, /Paid 250000 minor units/);
  assert.match(page, /First divergence Event 2/);
  assert.match(page, /total_refund_within_payment/);
  assert.match(page, /no_duplicate_full_refund/);
});

test("a violated invariant with no finding says so rather than inventing evidence", () => {
  const experiment = { ...toView(journey), findings: [] };
  const page = text(
    html(<DiffPanel campaignId="fx" experiment={experiment} origins={[]} baseline={null} fork={null} runOrigins={[]} />),
  );
  assert.match(page, /No verifier finding was supplied/);
  assert.match(page, /The verifier cites no failing event/);
});

test("evidence lists tools in order and links each finding to its events", () => {
  const page = html(
    <RunEvidence campaignId="fx" experiment={toView(journey)} run={runView(journey, "fork")} origins={journey.origins} focusEventId={null} />,
  );
  const order = [...page.matchAll(/data-testid="event-(\d+)"/g)].map((match) => Number(match[1]));
  assert.deepEqual(order, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
  assert.match(page, /href="#\/campaign\/fx\/evidence\?experiment=experiment-1&amp;run=fork&amp;event=12"/);
  assert.match(text(section(page, "event-10")), /First divergence/);
  assert.match(text(section(page, "event-12")), /Violation evidence/);
  assert.doesNotMatch(text(section(page, "event-10")).split("Event 10 request")[0] ?? "", /Violation evidence/);
});

test("a timeout outcome is visible in the refund evidence", () => {
  const page = text(
    html(<RunEvidence campaignId="fx" experiment={toView(refund)} run={runView(refund, "fork")} origins={[]} focusEventId={null} />),
  );
  assert.match(page, /issue_refund PaymentTimeout/);
  assert.match(page, /Payment committed; response timed out/);
});

test("repair_incomplete renders with no success styling or wording", () => {
  const { repair, outcome } = recorded.journey_repair_truncated;
  const markup = html(<RepairResult repair={repair} outcome={outcome} worldVersion="group-journey/1" origins={repairedOrigins} />);
  assert.match(markup, /data-testid="repair-verdict"/);
  assert.match(section(markup, "repair-verdict"), /Repair incomplete/);
  assert.doesNotMatch(markup, /data-tone="ok"/);
  const page = text(markup);
  assert.match(page, /run_status budget_exhausted/);
  assert.match(page, /safety_held true/);
  assert.match(page, /task_complete false/);
  assert.match(page, /verified false/);
  assert.match(page, /held_by not set/);
});

test("a verified repair shows what held it", () => {
  const { repair, outcome } = recorded.journey_repair_held_by_boundary;
  const markup = html(<RepairResult repair={repair} outcome={outcome} worldVersion="group-journey/1" origins={repairedOrigins} />);
  assert.match(markup, /data-tone="ok"/);
  const page = text(markup);
  assert.match(page, /held_by guarded_boundary/);
  assert.match(page, /Event 18: PermissionWithdrawnDenied/);
  assert.match(page, /enforced , guarded true/);
});

test("a reproduced failure is shown as a failure, with model_calls as recorded", () => {
  const markup = html(<ReplayPanel result={recorded.refund_replay} origins={repairedOrigins} />);
  assert.match(text(section(markup, "replay-fields")), /reproduced true .*model_calls 0/);
  assert.match(section(markup, "replay-verdict"), /Failure reproduced/);
  assert.doesNotMatch(markup, /data-tone="ok"/);
  assert.match(text(markup), /failed total_refund_within_payment/);
});

test("a capsule that did not reproduce is a failure", () => {
  const result = { ...recorded.journey_replay, reproduced: false };
  const markup = html(<ReplayPanel result={result} origins={repairedOrigins} />);
  assert.match(section(markup, "replay-verdict"), /data-tone="fail"/);
  assert.match(section(markup, "replay-verdict"), /Not reproduced/);
});

test("an enforced run with failing checks shows enforcement violated", () => {
  const markup = html(
    <EnforcementPanel
      execution={{ mode: "enforced", guarded: true }}
      checks={recorded.journey_replay.checks}
      reproduced
      checksMatch
      origins={repairedOrigins}
    />,
  );
  assert.match(text(section(markup, "enforcement-fields")), /mode enforced .*guarded true .*held false/);
  assert.match(markup, /data-tone="fail" data-testid="enforcement-verdict"/);
  assert.match(section(markup, "enforcement-verdict"), /Enforcement violated/);
  assert.match(text(markup), /failed delivered_messages_permitted/);
});

test("an enforced run with clean checks shows enforcement held", () => {
  const markup = html(
    <EnforcementPanel
      execution={{ mode: "enforced", guarded: true }}
      checks={{ delivered_messages_permitted: true }}
      reproduced
      checksMatch
      origins={repairedOrigins}
    />,
  );
  assert.match(text(section(markup, "enforcement-fields")), /held true/);
  assert.match(markup, /data-tone="ok" data-testid="enforcement-verdict"/);
  assert.match(section(markup, "enforcement-verdict"), /Enforcement held/);
});

test("a lab run shows no enforcement outcome, never a verdict", () => {
  const markup = html(
    <EnforcementPanel
      execution={{ mode: "lab", guarded: false }}
      checks={recorded.journey_replay.checks}
      reproduced
      checksMatch
      origins={repairedOrigins}
    />,
  );
  assert.match(markup, /data-tone="neutral" data-testid="enforcement-verdict"/);
  assert.match(section(markup, "enforcement-verdict"), /Lab run: enforcement was off/);
  assert.match(text(section(markup, "enforcement-fields")), /no enforcement outcome for this run/);
  assert.doesNotMatch(markup, /Enforcement (held|violated)/);
});

test("a diverged replay assesses no enforcement outcome", () => {
  const markup = html(
    <EnforcementPanel
      execution={{ mode: "enforced", guarded: true }}
      checks={{ delivered_messages_permitted: true }}
      reproduced={false}
      checksMatch={false}
      origins={repairedOrigins}
    />,
  );
  assert.match(markup, /data-tone="neutral" data-testid="enforcement-verdict"/);
  assert.match(section(markup, "enforcement-verdict"), /Replay diverged: enforcement not assessed/);
  assert.match(text(section(markup, "enforcement-fields")), /no enforcement outcome for this run/);
  assert.doesNotMatch(markup, /Enforcement (held|violated)/);
});

test("enforced mode with the guard off is not a lab run", () => {
  const markup = html(
    <EnforcementPanel
      execution={{ mode: "enforced", guarded: false }}
      checks={{ delivered_messages_permitted: true }}
      reproduced
      checksMatch
      origins={repairedOrigins}
    />,
  );
  assert.match(markup, /data-tone="neutral" data-testid="enforcement-verdict"/);
  assert.match(section(markup, "enforcement-verdict"), /Guard off: no enforcement outcome/);
  assert.doesNotMatch(section(markup, "enforcement-verdict"), /Lab run/);
});

test("a run with no recorded mode says so", () => {
  const markup = html(<EnforcementPanel execution={null} checks={recorded.journey_replay.checks} reproduced checksMatch origins={repairedOrigins} />);
  assert.match(section(markup, "enforcement-verdict"), /No recorded execution mode/);
  assert.match(text(section(markup, "enforcement-fields")), /not recorded/);
});

function extInspection(mode: unknown, failed: string[]): ExtInspection {
  return {
    details: mode === undefined ? {} : { enforced_execution: mode as never },
    external: [],
    failed_invariants: failed,
    replay_detail: "replay recorded actions against a reset environment",
    replay_kind: "recorded",
    replay_supported: true,
    schema_version: 3,
    truncated: false,
    world_version: "group-journey/1",
  };
}

test("the extensions inspection renders the enforcement outcome from its own details", () => {
  const violated = text(
    html(
      <EnforcementOutcome
        inspection={extInspection(
          { supported: true, detail: "an enforced run" },
          ["delivered_messages_permitted"],
        )}
      />,
    ),
  );
  assert.match(violated, /Enforcement violated/);
  assert.match(violated, /delivered_messages_permitted/);
  const held = text(
    html(<EnforcementOutcome inspection={extInspection({ supported: true, detail: "an enforced run" }, [])} />),
  );
  assert.match(held, /Enforcement held/);
});

test("the extensions inspection names why there is no enforcement outcome", () => {
  const lab = text(
    html(
      <EnforcementOutcome
        inspection={extInspection({ supported: false, detail: "recorded in lab mode" }, [])}
      />,
    ),
  );
  assert.match(lab, /no enforcement outcome/);
  assert.match(lab, /recorded in lab mode/);
  const missing = text(html(<EnforcementOutcome inspection={extInspection(undefined, [])} />));
  assert.match(missing, /carries no enforcement mode/);
});

const session = {
  schema_version: 1 as const,
  principal: { kind: "local_dev" as const, principal_id: "local-dev-user" },
  project_id: "project-local",
  auth_mode: "local_dev" as const,
  production_authenticated: false as const,
  can_approve: true,
  inference: "disabled" as const,
  runnable_worlds: [] as [],
};

const panel = (approval: ApprovalView, extra: Partial<Parameters<typeof DecisionPanel>[0]> = {}) =>
  html(
    <DecisionPanel
      approval={approval}
      session={session}
      busy={false}
      failure={null}
      result={null}
      origins={[]}
      onDecide={() => undefined}
      onResume={() => undefined}
      {...extra}
    />,
  );

test("the approval shows the exact action, scope and material risks", () => {
  const approval = scenario("fx-refund").stage.approval!;
  const markup = html(<ApprovalDetail approval={approval} origins={[]} />);
  const page = text(markup);
  assert.match(page, new RegExp(approval.request.action_digest));
  assert.match(page, /Changes which adapter later runs use/);
  assert.match(page, /project-local/);
  assert.match(page, /local-dev-user/);
  assert.match(section(markup, "exact-action"), /&quot;adapter&quot;: &quot;repaired&quot;/);
});

test("the journey approval shows the recorded execution mode and fixture digest", () => {
  const approval = scenario("fx-journey").stage.approval!;
  const page = text(html(<ApprovalDetail approval={approval} origins={[]} />));
  assert.match(page, /Execution mode enforced , guarded true/);
  assert.match(page, new RegExp(String(approval.action?.fixture_sha256)));
});

test("an approval with no readable action cannot be approved", () => {
  const approval = { ...scenario("fx-refund").stage.approval!, action: null };
  assert.match(text(html(<ApprovalDetail approval={approval} origins={[]} />)), /Do not approve what you cannot read/);
  assert.match(panel(approval), /disabled=""[^>]*>Approve this action/);
});

test("controls appear only for a pending approval", () => {
  const pending = scenario("fx-refund").stage.approval!;
  assert.match(panel(pending), /Approve this action/);
  const expired = { ...pending, effective_status: "expired" as const };
  assert.doesNotMatch(panel(expired), /Approve this action/);
  assert.match(text(panel(expired)), /This approval is expired/);
  const consumed = scenario("fx-refund").approved!.approval!;
  assert.doesNotMatch(panel(consumed), /Approve this action/);
  assert.match(text(panel(consumed)), /Approver \(from the session\) local-dev-user/);
});

test("an action in progress says what is happening, and an idle panel says nothing", () => {
  const pending = scenario("fx-refund").stage.approval!;
  assert.match(text(panel(pending, { busy: true, busyAction: "decide" })), /Recording your decision/);
  assert.match(text(panel(pending, { busy: true, busyAction: "resume" })), /Resuming the campaign/);
  assert.doesNotMatch(panel(pending), /Recording your decision|Resuming the campaign/);
});

test("a session without approval rights sees why, and the form never sends a role", () => {
  const pending = scenario("fx-refund").stage.approval!;
  const markup = panel(pending, { session: { ...session, can_approve: false } });
  assert.doesNotMatch(markup, /Approve this action/);
  assert.match(text(markup), /This session cannot approve/);
  assert.doesNotMatch(panel(pending), /name="(role|approver|principal)"/);
});

test("a denied decision is shown as denied and approves nothing", () => {
  const pending = scenario("fx-approval-denied").stage.approval!;
  const failure = scenario("fx-approval-denied").decisionFailure!;
  const markup = panel(pending, { failure });
  assert.match(markup, /data-testid="state-denied"/);
  assert.match(text(markup), /Denied .*approver_not_allowed/);
  assert.match(markup, /Approve this action/, "still pending, because the service said so");
});

test("a decision that could not resume says so and offers resume", () => {
  const pending = scenario("fx-resume-failed").stage.approval!;
  const result = {
    decision: { ...scenario("fx-refund").approved!.approval!.decision! },
    approval: pending.request,
    resumed: false,
    resume_error: scenario("fx-resume-failed").resumeError!,
  };
  const page = text(panel(pending, { result }));
  assert.match(page, /Decision recorded, campaign not resumed/);
  assert.match(page, /settings_changed/);
  assert.match(page, /Resume campaign/);
});

test("every required state has a visible notice", () => {
  const failures: Array<[Failure, string]> = [
    [scenario("fx-load-denied").loadFailure!, "denied"],
    [scenario("fx-load-invalid").loadFailure!, "validation_error"],
    [scenario("fx-load-missing").loadFailure!, "not_found"],
    [{ kind: "transport", message: "offline" }, "reconnecting"],
    [{ kind: "invalid_response", operation: "getCampaign", message: "bad" }, "failed"],
  ];
  for (const [failure, state] of failures) {
    assert.match(html(<FailureNotice failure={failure} />), new RegExp(`data-testid="state-${state}"`));
  }
  for (const state of ["loading", "empty", "cancelled", "awaiting_approval"] as const) {
    assert.match(html(<Notice state={state} title="t" />), new RegExp(`data-testid="state-${state}"`));
  }
});

test("empty evidence reads as empty", () => {
  const markup = text(html(<ApplicationEvents events={[]} origins={[]} campaignId="fx" experimentId={null} />));
  assert.match(markup, /No campaign events yet/);
  const none = text(html(<FindingList findings={[]} campaignId="fx" experimentId="e" run="fork" />));
  assert.match(none, /No findings for this run/);
});

test("campaign event text renders as data", () => {
  const event = { ...scenario("fx-running").stage.events[0]!, summary: "<img src=x onerror=alert(1)>" };
  const markup = html(<ApplicationEvents events={[event]} origins={[]} campaignId="fx" experimentId={null} />);
  assert.doesNotMatch(markup, /<img/);
  assert.match(markup, /&lt;img/);
});

test("test data is labelled and service data is not", () => {
  assert.match(html(<OriginNote origins={refund.origins} />), /Test data/);
  assert.equal(html(<OriginNote origins={[{ kind: "service" }]} />), "");
  assert.match(text(html(<TestDataBanner />)), /not a live or integrated result/);
});

test("the fixture app always carries the test data banner; the service app does not", () => {
  const fixture = createFixtureSource();
  const withHarness = html(<App source={fixture} harness={fixture.harness} />);
  assert.match(withHarness, /data-testid="test-data-banner"/);
  const service = { ...fixture, kind: "api" as const };
  assert.doesNotMatch(html(<App source={service} harness={null} />), /test-data-banner/);
});
test("campaign progress shows live counts and whether the view is updating", () => {
  const running = scenario("fx-running").stage.view;
  const markup = html(<ProgressPanel view={running} origins={[]} live={{ connection: "live", cursor: 5 }} />);
  assert.match(markup, /data-testid="campaign-progress"/);
  const page = text(markup);
  assert.match(page, /Running/);
  assert.match(page, /3 tasks: 2 succeeded, 1 in progress/);
  assert.match(page, /1 experiment/);
  assert.match(page, /Events 5 of 5 shown/);
  assert.match(page, /Worker active/);
  assert.match(page, /Updating from the service/);
  const done = scenario("fx-refund").approved!.view;
  const settled = text(html(<ProgressPanel view={done} origins={[]} live={{ connection: "live", cursor: done.event_count }} />));
  assert.match(settled, /No further updates are expected/);
  const stale = text(html(<ProgressPanel view={running} origins={[]} live={{ connection: "reconnecting", cursor: 5 }} />));
  assert.match(stale, /Reconnecting/);
});

test("campaign events render as a labelled timeline", () => {
  const events = scenario("fx-refund").stage.events;
  const markup = html(<ApplicationEvents events={events} origins={[]} campaignId="fx-refund" experimentId="experiment-1" />);
  assert.match(markup, /data-testid="event-timeline"/);
  const page = text(markup);
  assert.match(page, /Campaign created/);
  assert.match(page, /Run started/);
  assert.match(page, /Finding recorded/);
  assert.match(page, /Approval requested/);
  assert.match(page, /2026-10-29 10:00:01 UTC/);
  assert.match(page, /task fx-refund-i1-validate/);
  assert.match(page, /run run-faulted-1/);
});

test("the diff summary counts differing records and names changed metrics", () => {
  const markup = diffPanel(refund);
  assert.match(markup, /data-testid="diff-summary"/);
  const page = text(markup);
  assert.match(page, /Compared \d+ event records; \d+ differ in request, outcome or state/);
  assert.match(page, /First divergence at event 2/);
  assert.match(page, /failing event/);
  assert.match(page, /Recorded metrics that differ: refunded_minor/);
  assert.match(markup, /data-testid="difference-count"/);
  assert.match(page, /recorded fields differ at this event/);
  assert.match(markup, /data-testid="ledger-comparison"/);
  assert.match(page, /Baseline refunds 1 transaction; fork refunds 2 transactions/);
  assert.match(page, /Only in fork: refund-2/);
  assert.match(page, /Change \(computed\)/);
  assert.match(page, /Differs/);
  assert.match(page, /computed from the two recorded values/);
});

const demoSource = { kind: "api" } as unknown as DataSource;
const demoSession: SessionInfo = { ...session, inference: "scripted", runnable_worlds: ["refund-world/1"] };

test("the guided demo offers one click on synthetic data", () => {
  const markup = html(<GuidedDemo source={demoSource} session={demoSession} />);
  assert.match(markup, /data-testid="guided-demo"/);
  const page = text(markup);
  assert.match(page, /Start guided demo run/);
  assert.match(page, /Synthetic world: no real people or money/);
  assert.match(page, /Proposals scripted: checked-in replies, not a model/);
  assert.match(page, /Watch campaign progress while the baseline and fork run/);
  assert.match(page, /Scripted scenario default/);
  assert.match(page, /refund-world\/1/);
});

test("the guided demo stays honest when it cannot run", () => {
  const disabled = html(<GuidedDemo source={demoSource} session={{ ...demoSession, inference: "disabled" }} />);
  assert.match(disabled, /disabled=""/);
  assert.match(text(disabled), /cannot start new campaigns/);
  assert.equal(html(<GuidedDemo source={demoSource} session={{ ...demoSession, runnable_worlds: [] }} />), "");
});
