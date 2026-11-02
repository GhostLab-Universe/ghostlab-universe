/* Step 6: run the guided config and inspect the recorded evidence. */

import { useMemo, useState, type ReactNode } from "react";
import { validateEffectiveConfig } from "../config";
import {
  banner,
  button,
  checksTable,
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
  traceTable,
  type StepContext,
} from "../ui";

export function Step6(props: { ctx: StepContext }): ReactNode {
  const { ctx } = props;
  const validation = useMemo(() => validateEffectiveConfig(ctx.config), [ctx.config]);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const evidence = ctx.evidence;

  const run = (): void => {
    setRunning(true);
    setRunError(null);
    ctx.api
      .runExperiment(ctx.config)
      .then((record) => ctx.setEvidence(record))
      .catch((error: unknown) => setRunError(error instanceof Error ? error.message : String(error)))
      .finally(() => setRunning(false));
  };

  if (!validation.ok) {
    return div(
      null,
      h1(null, "Step 6: Run & inspect evidence"),
      errorList(validation.errors),
      p(null, "Fix the config in ", navLink(ctx.navigate, "/guide/4-configure", "step 4"), " before running."),
    );
  }

  return div(
    null,
    h1(null, "Step 6: Run & inspect evidence"),
    div(
      { className: "row" },
      button({ onClick: run, label: running ? "Running…" : evidence === null ? "Run guided experiment" : "Run again", disabled: running, testid: "run-button" }),
    ),
    runError !== null ? banner("error", "Run failed", runError) : null,
    evidence === null
      ? p(null, "No run yet. The run submits the reviewed config and records it with the evidence.")
      : div(
          null,
          banner("warn", `Mode: ${evidence.mode}`, evidence.notices.join(" ")),
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
