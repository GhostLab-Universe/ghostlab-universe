/* Step 7: export the recorded evidence, compare runs, plan a retest. */

import { useMemo, type ReactNode } from "react";
import {
  banner,
  button,
  code,
  codeBlock,
  div,
  divergenceView,
  fmtMinor,
  h1,
  h2,
  kvTable,
  navLink,
  p,
  table,
  tbody,
  td,
  th,
  thead,
  tr,
  type StepContext,
} from "../ui";

function download(name: string, mediaType: string, content: string): void {
  const blob = new Blob([content], { type: mediaType });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export function Step7(props: { ctx: StepContext }): ReactNode {
  const { ctx } = props;
  const evidence = ctx.evidence;
  const bundle = useMemo(
    () => (evidence === null ? null : ctx.api.exportBundle(evidence)),
    [ctx.api, evidence],
  );

  if (evidence === null || bundle === null) {
    return div(
      null,
      h1(null, "Step 7: Export, compare & retest"),
      banner("info", "Nothing to export yet", "Run the guided experiment in step 6 first."),
      p(null, navLink(ctx.navigate, "/guide/6-run", "← Back to run")),
    );
  }

  const readme = bundle.files.find((f) => f.name === "README.txt");
  const evidenceFile = bundle.files.find((f) => f.name === "evidence.json");
  const currency = evidence.result.baseline.final_state.order.currency;

  return div(
    null,
    h1(null, "Step 7: Export, compare & retest"),
    banner("ok", "No credentials in this bundle", "Exports carry the synthetic config and evidence only. The acceptance suite scans every export for secret patterns."),
    h2(null, "Export bundle"),
    p({ "data-testid": "export-label" }, `Mode ${evidence.mode}; submitted ${evidence.submittedAt}.`),
    table(
      { className: "trace", "data-testid": "export-table" },
      thead(null, tr(null, th(null, "File"), th(null, "Size (bytes)"), th(null, "Download"))),
      tbody(
        null,
        ...bundle.files.map((file) =>
          tr(
            { key: file.name },
            td(null, code(null, file.name)),
            td(null, String(new TextEncoder().encode(file.content).length)),
            td(null, button({ onClick: () => download(file.name, file.mediaType, file.content), label: `Download ${file.name}`, testid: `download-${file.name}` })),
          ),
        ),
      ),
    ),
    h2(null, "Bundle readme"),
    codeBlock(readme?.content ?? "", "readme-preview"),
    h2(null, "Evidence preview"),
    codeBlock(evidenceFile?.content ?? "", "export-preview"),
    h2(null, "Compare baseline vs faulted"),
    p({ "data-testid": "compare-label" }, evidence.source.experimentResult),
    kvTable([
      ["Baseline total", fmtMinor(evidence.result.baseline.final_state.total_refunded_minor, currency)],
      ["Faulted total", fmtMinor(evidence.result.faulted.final_state.total_refunded_minor, currency)],
      ["Repaired total", fmtMinor(evidence.result.repaired.final_state.total_refunded_minor, currency)],
      ["Violated invariants", evidence.result.diff.violated_invariants.join(", ") || "none"],
    ]),
    divergenceView(evidence.result.diff.first_divergence),
    h2(null, "Retest"),
    p(
      null,
      "Replay the approved fixture capsule with the real replay tooling (zero model calls). ",
      "Run from the repository root with PYTHONPATH=src:",
    ),
    codeBlock("python -m ghostlab replay examples/contracts/core/capsule_v1.refund_failure.json", "retest-command"),
    p(null, "Live retest of this exact config unlocks with the service integration; the recorded config above is the retest input."),
    p(null, navLink(ctx.navigate, "/guide/6-run", "← Back to run")),
  );
}
