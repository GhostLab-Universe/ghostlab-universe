import assert from "node:assert/strict";
import { test } from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import type { ReactNode } from "react";
import type { ExperimentView, RunView } from "../../src/api/api.generated.ts";
import { createFixtureSource } from "../../src/data/fixtureSource.ts";
import type { Failure } from "../../src/data/source.ts";
import { recorded } from "../../src/fixtures/recorded.generated.ts";
import { SCENARIOS, type ExperimentFixture } from "../../src/fixtures/scenarios.ts";
import { ServiceModeNotice, SessionFacts } from "../../src/ui/App.tsx";
import { FailureNotice, RecordedErrors } from "../../src/ui/components.tsx";
import { DecisionPanel } from "../../src/views/ApprovalPage.tsx";
import { CampaignPanel, RepairResult } from "../../src/views/CampaignPage.tsx";
import { ReplayPanel } from "../../src/views/CapsulePage.tsx";
import { DiffPanel } from "../../src/views/DiffPage.tsx";
import { ApplicationEvents, ProposalSection, RunEvidence } from "../../src/views/EvidencePage.tsx";

const html = (node: ReactNode) => renderToStaticMarkup(node);
const text = (markup: string) => markup.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
const scenario = (id: string) => SCENARIOS.find((item) => item.id === id)!;
const source = createFixtureSource();
const refresh = async () => undefined;

function campaignPage(id: string) {
  const { view, origins } = scenario(id).stage;
  return html(<CampaignPanel view={view} origins={origins} source={source} refresh={refresh} />);
}

function experimentView(experiment: ExperimentFixture, withCoverage = true): ExperimentView {
  const fields = experiment;
  return {
    experiment_id: fields.experiment_id,
    iteration: 1,
    proposal: fields.proposal,
    execution: fields.execution,
    diff: fields.diff,
    findings: fields.findings,
    runs: Object.keys(fields.runs) as ["baseline", "fork"],
    coverage: withCoverage ? fields.coverage : null,
  };
}

function runView(experiment: ExperimentFixture, name: "baseline" | "fork"): RunView {
  const run = experiment.runs[name]!;
  return {
    run_id: run.run_id,
    experiment_id: experiment.experiment_id,
    run: name,
    artifact: { kind: "artifact", id: "artifact-1", sha256: null },
    execution: experiment.execution,
    result: run.result,
    findings: run.findings,
  };
}

const diffPage = (experiment: ExperimentFixture, withCoverage = true) =>
  html(
    <DiffPanel
      campaignId="fx"
      experiment={experimentView(experiment, withCoverage)}
      origins={experiment.origins}
      baseline={runView(experiment, "baseline")}
      fork={runView(experiment, "fork")}
      runOrigins={experiment.origins}
    />,
  );

test("the campaign view shows coverage_incomplete and no success", () => {
  const page = campaignPage("fx-coverage-incomplete");
  assert.match(page, /data-testid="state-incomplete"/);
  assert.doesNotMatch(page, /data-tone="ok"/);
  const shown = text(page);
  assert.match(shown, /A run that stopped early proves nothing/);
  assert.match(shown, /Coverage: baseline budget_exhausted , fork budget_exhausted , complete false/);
  assert.match(shown, /Stopped before finishing/);
  assert.match(shown, /stopped at their tool cap/);
});

test("the campaign view says when coverage was not recorded", () => {
  const { view, origins } = scenario("fx-coverage-incomplete").stage;
  const experiments = view.experiments.map((experiment) => ({ ...experiment, coverage: null }));
  const page = text(html(<CampaignPanel view={{ ...view, experiments }} origins={origins} source={source} refresh={refresh} />));
  assert.match(page, /Coverage was not recorded for this experiment/);
  assert.match(page, /proves nothing/, "the outcome still says so without the field");
});

test("the diff view shows truncation with no failure", () => {
  const page = diffPage(scenario("fx-coverage-incomplete").stage.experiment!);
  assert.match(page, /data-testid="state-incomplete"/);
  assert.match(text(page), /Coverage incomplete/);
  assert.match(text(page), /No divergence recorded/);
  assert.doesNotMatch(page, /data-tone="ok"/);
});

test("the diff view keeps a violation seen before truncation", () => {
  const page = text(diffPage(scenario("fx-coverage-violation").stage.experiment!));
  assert.match(page, /Coverage incomplete, violation recorded/);
  assert.match(page, /First divergence Event 3/);
  assert.match(page, /Violation evidence Event 10/);
  assert.match(page, /delivered_messages_permitted/);
});

test("on an older checkpoint the diff reads coverage from the runs", () => {
  const page = text(diffPage(scenario("fx-coverage-violation").stage.experiment!, false));
  assert.match(page, /Coverage incomplete, violation recorded/);
  assert.match(page, /read from the two recorded runs/);
});

test("a complete experiment shows its coverage without a warning", () => {
  const page = diffPage(scenario("fx-refund").stage.experiment!);
  assert.doesNotMatch(page, /data-testid="state-incomplete"/);
  assert.match(text(page), /complete true/);
});

test("a truncated run says so in the evidence view, even with passing checks", () => {
  const experiment = scenario("fx-coverage-incomplete").stage.experiment!;
  const page = html(
    <RunEvidence campaignId="fx" experiment={experimentView(experiment)} run={runView(experiment, "fork")} origins={[]} focusEventId={null} />,
  );
  assert.match(page, /data-testid="state-incomplete"/);
  assert.match(text(page), /This run stopped before finishing/);
  assert.match(text(page), /Passing checks on it prove nothing/);
});

test("repair_incomplete renders without success at either campaign status", () => {
  for (const id of ["fx-journey-truncated", "fx-journey-idle"]) {
    const page = campaignPage(id);
    assert.doesNotMatch(page, /data-tone="ok"/, id);
    assert.match(text(page), /Repair incomplete/, id);
    assert.match(text(page), /Outcome|held_by not set/, id);
  }
  const { repair, outcome } = recorded.journey_repair_idle;
  const page = text(html(<RepairResult repair={repair} outcome={outcome} worldVersion="group-journey/1" origins={[]} />));
  assert.match(page, /run_status completed/);
  assert.match(page, /task_complete false/);
  assert.match(page, /held_by not set/);
});

test("a resume refused with settings_changed is blocked, not failed", () => {
  const failure: Failure = {
    kind: "service",
    status: 409,
    reason: "settings_changed",
    error: { code: "conflict", message: "the world fixture changed", ref: null, retryable: false, side_effect: "none" },
  };
  const markup = html(<FailureNotice failure={failure} />);
  assert.match(markup, /data-testid="state-blocked"/);
  assert.doesNotMatch(markup, /state-failed/);
  assert.match(text(markup), /any approval is still unused/);
  assert.match(text(markup), /A damaged record can't be restored/);
});

test("a failed campaign with outcome settings_changed is blocked, and resume isn't offered", () => {
  const page = campaignPage("fx-settings-blocked");
  assert.match(page, /data-testid="state-blocked"/);
  assert.doesNotMatch(page, /data-testid="state-failed"/);
  assert.doesNotMatch(text(page), /Resume campaign/, "resume does nothing on a finished campaign");
  assert.match(text(page), /The approval is unspent, and this campaign can't continue/);
});

test("the campaign view shows the pinned fixture, or says it is older", () => {
  const { view, origins } = scenario("fx-journey").stage;
  assert.match(text(campaignPage("fx-journey")), new RegExp(view.fixture_sha256 ?? "missing"));
  const older = html(<CampaignPanel view={{ ...view, fixture_sha256: null }} origins={origins} source={source} refresh={refresh} />);
  assert.match(text(older), /not recorded on this older checkpoint/);
});

test("a replay of a truncated run is never read as a pass", () => {
  const stage = scenario("fx-coverage-incomplete").stage;
  const markup = html(<ReplayPanel result={stage.experiment!.replay} origins={[]} />);
  assert.match(text(markup), /reproduced true/);
  assert.match(text(markup), /run_status budget_exhausted/);
  assert.match(markup, /data-tone="warn"[^>]*data-testid="replay-verdict"/);
  assert.match(text(markup), /Passing checks on it prove nothing/);
  assert.doesNotMatch(markup, /data-tone="ok"/);
});

test("a replay reaching a violation in a truncated run keeps it", () => {
  const stage = scenario("fx-coverage-violation").stage;
  assert.match(text(html(<ReplayPanel result={stage.experiment!.replay} origins={[]} />)), /The failure it reached still stands/);
});

const panel = (id: string, extra: Partial<Parameters<typeof DecisionPanel>[0]>) =>
  html(
    <DecisionPanel
      approval={scenario(id).stage.approval!}
      session={null}
      busy={false}
      failure={null}
      result={null}
      origins={[]}
      onDecide={() => undefined}
      onResume={() => undefined}
      {...extra}
    />,
  );

test("the decision panel keeps a blocked resume visible", () => {
  const stage = scenario("fx-resume-failed").stage;
  const result = {
    decision: scenario("fx-refund").approved!.approval!.decision!,
    approval: stage.approval!.request,
    resumed: false,
    resume_error: scenario("fx-resume-failed").resumeError!,
  };
  const markup = panel("fx-resume-failed", { result });
  assert.match(markup, /data-testid="state-blocked"/);
  assert.match(text(markup), /Decision recorded, campaign not resumed/);
  assert.match(text(markup), /settings_changed/);
  assert.match(text(markup), /any approval is still unused/);
});

test("after a reload, an unspent approval still explains itself", () => {
  const blocked = panel("fx-settings-blocked", { blocked: true });
  assert.match(blocked, /data-testid="state-blocked"/);
  assert.doesNotMatch(text(blocked), /Resume campaign/);
  const unspent = text(panel("fx-settings-blocked", { unspent: true }));
  assert.match(unspent, /Approved, not yet used/);
  assert.match(unspent, /Resume campaign/);
});

test("redacted text renders plainly everywhere it can appear", () => {
  const stage = scenario("fx-redacted").stage;
  const events = html(<ApplicationEvents events={stage.events} origins={[]} campaignId="fx" experimentId={null} />);
  assert.match(text(events), /Explorer proposed: Retrying with key \[REDACTED\] issues a second refund \(secret-shaped text redacted\)/);
  const errors = text(html(<RecordedErrors errors={stage.view.errors} />));
  assert.match(errors, /Provider said token=\[REDACTED\]/);
  const proposal = text(html(<ProposalSection experiment={experimentView(stage.experiment!)} origins={[]} />));
  assert.match(proposal, /Retrying with key \[REDACTED\]/);
  const campaign = campaignPage("fx-redacted");
  assert.match(text(campaign), /Retrying with key \[REDACTED\]/);
  assert.doesNotMatch(campaign, /data-testid="state-failed"/, "redaction is not a failure");
});

const session = {
  schema_version: 1 as const,
  principal: { kind: "local_dev" as const, principal_id: "local-dev-user" },
  project_id: "project-local",
  auth_mode: "local_dev" as const,
  production_authenticated: false as const,
  can_approve: true,
  inference: "fixture" as const,
  runnable_worlds: [] as [],
};

test("the session's three modes are always shown as recorded", () => {
  const facts = text(html(<SessionFacts session={session} />));
  assert.match(facts, /auth_mode local_dev/);
  assert.match(facts, /production_authenticated false/);
  assert.match(facts, /inference fixture/);
  const disabled = text(html(<SessionFacts session={{ ...session, inference: "disabled" }} />));
  assert.match(disabled, /inference disabled/);
});

test("scripted inference behind the service is called out", () => {
  assert.match(text(html(<ServiceModeNotice session={session} />)), /scripted test data, not a model/);
  assert.match(text(html(<ServiceModeNotice session={{ ...session, inference: "disabled" }} />)), /cannot start new campaigns/);
  assert.equal(html(<ServiceModeNotice session={{ ...session, inference: "live" }} />), "");
});
