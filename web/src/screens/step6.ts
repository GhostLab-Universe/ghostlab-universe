/* Step 6: the Muse hunt story (HuntFlow, live-model routes) above the guided
 * single run of the step 5 config and its recorded evidence. */

import { createElement, useMemo, useState, type ReactNode } from "react";
import { HuntFlow } from "./hunt";
import { validateEffectiveConfig } from "../config";
import { isServiceGuide } from "../serviceClient";
import type { ArmReference } from "../types";
import {
  banner,
  button,
  checksTable,
  code,
  codeBlock,
  div,
  divergenceView,
  errorList,
  fmtMinor,
  h,
  h1,
  h2,
  kvTable,
  ledgerTable,
  navLink,
  p,
  table,
  tbody,
  td,
  th,
  thead,
  traceTable,
  tr,
  type StepContext,
} from "../ui";

function armKindNote(variant: ArmReference["variant"]): string {
  if (variant === "repaired") {
    return "Fresh live execution of the repaired deterministic adapter with the fault armed (not a capsule retest, not boundary enforcement).";
  }
  if (variant === "baseline") return "Fresh live execution, no faults armed.";
  if (variant === "faulted") return "Fresh live execution of the vulnerable adapter with the fault armed.";
  return "Fresh live execution of the submitted variant (matches no canonical branch).";
}

export function Step6(props: { ctx: StepContext }): ReactNode {
  const { ctx } = props;
  const validation = useMemo(() => validateEffectiveConfig(ctx.config), [ctx.config]);
  const [running, setRunning] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const evidence = ctx.evidence;
  const service = isServiceGuide(ctx.api);

  const run = (): void => {
    setRunning(true);
    setRunError(null);
    ctx.api
      .runExperiment(ctx.config)
      .then((record) => ctx.setEvidence(record))
      .catch((error: unknown) => setRunError(error instanceof Error ? error.message : String(error)))
      .finally(() => setRunning(false));
  };

  const cancel = (): void => {
    if (!isServiceGuide(ctx.api)) return;
    const api = ctx.api;
    setCancelling(true);
    api
      .cancelRun()
      .then((groups) => {
        ctx.notify(groups.length === 0 ? "Nothing to cancel." : `Cancel requested for ${String(groups.length)} group(s).`);
      })
      .catch((error: unknown) => setRunError(error instanceof Error ? error.message : String(error)))
      .finally(() => setCancelling(false));
  };

  const hunt = createElement(HuntFlow, { base: ctx.service?.baseUrl ?? "" });
  if (!validation.ok) {
    return div(
      null,
      h1(null, "Step 6: Hunt, prove & fix"),
      hunt,
      h2({ className: "guided-head" }, "Guided config run (step 5 config)"),
      errorList(validation.errors),
      p(null, "Fix the config in ", navLink(ctx.navigate, "/guide/4-configure", "step 4"), " before running."),
    );
  }

  const refs = evidence?.references ?? null;
  const provenance = evidence?.provenance ?? null;

  const recoveryPanel =
    service && ctx.recovery.length > 0
      ? div(
          null,
          h2(null, "Stored runs (reload recovery)"),
          p(null, "Handles below were re-read via the API and digest-verified. Loading one restores its submitted config and evidence."),
          table(
            { className: "trace", "data-testid": "recovery-table" },
            thead(null, tr(null, th(null, "Stored"), th(null, "Arms"), th(null, "Verified"), th(null, "Load"))),
            tbody(
              null,
              ...ctx.recovery.map((outcome, index) => {
                const key = `${outcome.stored.storedAt}-${String(index)}`;
                return tr(
                  { key },
                  td(null, outcome.stored.storedAt),
                  td(null, outcome.stored.arms.map((arm) => arm.variant).join(", ")),
                  td(null, outcome.verified ? "verified (digests match)" : `FAILED: ${outcome.problems.join(" | ")}`),
                  td(
                    null,
                    button({
                      onClick: () => {
                        ctx.replaceConfig(outcome.stored.submittedConfig);
                        ctx.setEvidence(outcome.stored.evidence);
                        ctx.notify(outcome.verified ? "Stored run loaded (verified)." : "Stored run loaded UNVERIFIED — see problems.");
                      },
                      label: "Load",
                      testid: `recovery-load-${String(index)}`,
                    }),
                  ),
                );
              }),
            ),
          ),
          button({ onClick: ctx.refreshRecovery, label: "Re-verify stored runs", testid: "recovery-refresh" }),
        )
      : null;

  const referencesPanel =
    refs === null
      ? null
      : div(
          null,
          h2(null, "Executed arms + references"),
          p(
            { "data-testid": "run-references" },
            `Every arm below is a real service execution with service-issued ids (project ${refs.projectId}).`,
          ),
          table(
            { className: "trace", "data-testid": "arms-table" },
            thead(null, tr(null, th(null, "Arm"), th(null, "Run"), th(null, "Group"), th(null, "Status"), th(null, "Verdict"), th(null, "Model calls"), th(null, "Tool calls"))),
            tbody(
              null,
              ...refs.arms.map((arm) =>
                tr(
                  { key: arm.runId },
                  td(null, code(null, arm.variant)),
                  td(null, code(null, arm.runId)),
                  td(null, code(null, arm.groupId)),
                  td(null, arm.status),
                  td(null, arm.verdict),
                  td(null, String(arm.modelCalls)),
                  td(null, String(arm.toolCalls)),
                ),
              ),
            ),
          ),
          ...refs.arms.map((arm) => p({ key: `note-${arm.runId}` }, `${arm.variant}: ${armKindNote(arm.variant)}`)),
          refs.pins === null
            ? null
            : div(
                null,
                h2(null, "Pinned versions (resolution echo)"),
                kvTable([
                  ["Environment", `${refs.pins.envId} (${refs.pins.envVersion})`],
                  ["Adapter", `${refs.pins.adapterRef} (${refs.pins.adapterVersion})`],
                  ["Code digest", code(null, refs.pins.codeDigest)],
                  ["Manifest digest", refs.pins.manifestDigest ?? "none"],
                  ["World", refs.pins.worldVersion],
                  ["Controller", refs.pins.controllerVersion],
                ]),
              ),
          h2(null, "Mapping notes (submit echo)"),
          h(
            "details",
            null,
            h("summary", null, `${String(refs.mappingNotes.length)} mapping notes; the runtime check for the step-5 audit`),
            codeBlock(JSON.stringify(refs.mappingNotes, null, 2), "mapping-notes"),
          ),
        );

  const provenancePanel =
    provenance === null
      ? null
      : div(
          null,
          h2(null, "Provenance (server-derived)"),
          kvTable([
            ["Environment", provenance.environment],
            ["Target", provenance.target],
            ["Explorer", provenance.explorer],
            ["Evidence", provenance.evidence],
            ["Model calls (sum)", String(provenance.modelCalls)],
            ["Spend", provenance.spend],
          ]),
        );

  return div(
    null,
    h1(null, "Step 6: Hunt, prove & fix"),
    hunt,
    h2({ className: "guided-head" }, "Guided config run (step 5 config)"),
    div(
      { className: "row" },
      button({ onClick: run, label: running ? "Running…" : evidence === null ? "Run guided experiment" : "Run again", disabled: running, testid: "run-button" }),
      service && running
        ? button({ onClick: cancel, label: cancelling ? "Cancelling…" : "Cancel run", disabled: cancelling, testid: "cancel-button" })
        : null,
    ),
    runError !== null ? banner("error", "Run failed", runError) : null,
    runError !== null && service ? p({ "data-testid": "service-error" }, runError) : null,
    runError !== null && service
      ? p(null, "The config is preserved above (step 5) — fix the cause and run again. No fixtures were substituted.")
      : null,
    runError !== null && service && runError.includes("already recorded")
      ? p(
          null,
          "This exact config already executed: the run route cannot start a new attempt for an identical config. ",
          "Change a recorded field (e.g. the objective name) to run again, or load the recorded run from recovery above. ",
          "New-attempt support on POST /v1/runs is pending on the service.",
        )
      : null,
    recoveryPanel,
    evidence === null
      ? p(null, "No run yet. The run submits the reviewed config and records it with the evidence.")
      : div(
          null,
          banner("warn", `Mode: ${evidence.mode}`, evidence.notices.join(" ")),
          referencesPanel,
          provenancePanel,
          h2(null, "Recorded config (echoed by the run)"),
          p({ "data-testid": "recorded-at" }, `Submitted at ${evidence.submittedAt}.`),
          codeBlock(JSON.stringify(evidence.effectiveConfig, null, 2), "recorded-config"),
          h2(null, "Ledger totals (minor units)"),
          p({ "data-testid": "totals-label" }, evidence.source.experimentResult),
          kvTable([
            ["Baseline", fmtMinor(evidence.result.baseline.final_state.total_refunded_minor, evidence.result.baseline.final_state.order.currency)],
            ["Faulted", fmtMinor(evidence.result.faulted.final_state.total_refunded_minor, evidence.result.faulted.final_state.order.currency)],
            ["Repaired", fmtMinor(evidence.result.repaired.final_state.total_refunded_minor, evidence.result.repaired.final_state.order.currency)],
            ["Violated invariants", evidence.result.diff.violated_invariants.join(", ") || "none"],
          ]),
          divergenceView(evidence.result.diff.first_divergence),
          h2(null, "Verifier checks (from evidence, not frontend arithmetic)"),
          h("details", null, h("summary", null, "Baseline checks"), checksTable(evidence.result.baseline.checks, evidence.effectiveConfig.checks.invariants)),
          h("details", { open: true }, h("summary", null, "Faulted checks"), checksTable(evidence.result.faulted.checks, evidence.effectiveConfig.checks.invariants)),
          h("details", null, h("summary", null, "Repaired checks"), checksTable(evidence.result.repaired.checks, evidence.effectiveConfig.checks.invariants)),
          h2(null, "Invariant finding"),
          p({ "data-testid": "finding-label" }, evidence.source.capsule),
          kvTable([
            ["Finding", evidence.finding.finding_id],
            ["Invariant", evidence.finding.invariant],
            ["Verdict", evidence.finding.verdict],
            ["Observed / limit (minor)", `${String(evidence.finding.observed_minor)} / ${String(evidence.finding.limit_minor)}`],
            ["Evidence events", evidence.finding.evidence_event_ids.join(", ")],
            ["Verifier", evidence.finding.verifier_version],
          ]),
          h2(null, "Faulted ledger"),
          ledgerTable(evidence.result.faulted.final_state.ledger),
          h2(null, "Ordered traces"),
          h("details", null, h("summary", null, "Baseline trace"), traceTable(evidence.result.baseline.trace, "trace-baseline")),
          h("details", { open: true }, h("summary", null, "Faulted trace"), traceTable(evidence.result.faulted.trace, "trace-faulted")),
          h("details", null, h("summary", null, "Repaired trace"), traceTable(evidence.result.repaired.trace, "trace-repaired")),
        ),
    p(null, navLink(ctx.navigate, "/guide/7-export", "Continue to export →")),
  );
}
