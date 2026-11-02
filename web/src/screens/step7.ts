/* Step 7: export the recorded evidence, compare runs, plan a retest. */

import { useMemo, useState, type ReactNode } from "react";
import type { EffectiveConfig } from "../config";
import { isServiceGuide, type ServerExportBundle } from "../serviceClient";
import type { ArmReference, ExperimentEvidence, ExportBundle } from "../types";
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
  radioRow,
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

/** The variant config the service executed for one arm. */
function variantConfig(submitted: EffectiveConfig, variant: ArmReference["variant"]): EffectiveConfig {
  if (variant === "submitted-extra") return submitted;
  const adapter = variant === "repaired" ? "repaired" : "vulnerable";
  const timeoutAfterCommit = variant !== "baseline";
  return {
    ...JSON.parse(JSON.stringify(submitted)) as EffectiveConfig,
    agent: { ...submitted.agent, adapter },
    faults: { timeoutAfterCommit },
  };
}

function serviceFiles(evidence: ExperimentEvidence, arm: ArmReference, server: ServerExportBundle): ExportBundle {
  const echoMatches =
    server.effectiveConfig !== null &&
    JSON.stringify(server.effectiveConfig) === JSON.stringify(variantConfig(evidence.effectiveConfig, arm.variant));
  const evidenceDoc = {
    mode: evidence.mode,
    submittedAt: evidence.submittedAt,
    selectedArm: arm.variant,
    source: evidence.source,
    notices: evidence.notices,
    references: evidence.references ?? null,
    provenance: evidence.provenance ?? null,
    serverExport: {
      group_id: server.groupId,
      resolved_id: server.resolvedId,
      requested_id: server.requestedId,
      status: server.status,
      exported_at: server.exportedAt,
      digests: server.digests,
      mapping_notes: server.mappingNotes,
      runs: server.runs,
      resolved: server.resolved,
    },
    effectiveEchoMatches: echoMatches,
    finding: evidence.finding,
    findingOrigin: "assembled in the browser from executed evidence (see step-6 notices); not server-issued",
  };
  const readme = [
    "GhostLab guided-run evidence bundle (live service).",
    "",
    `mode: ${evidence.mode}`,
    `submittedAt: ${evidence.submittedAt}`,
    `selected arm: ${arm.variant}`,
    `group: ${server.groupId}`,
    `resolved: ${server.resolvedId}`,
    `requested: ${server.requestedId}`,
    `run: ${arm.runId} (status ${arm.status}, verdict ${arm.verdict})`,
    `effective echo matches the ${arm.variant} variant: ${echoMatches ? "yes" : "NO — investigate before trusting this bundle"}`,
    "",
    "Files:",
    "- server-export.json: the raw GET export bundle (actual recorded artifacts: resolved config, runs, echoes, digests).",
    "- effective-config.json: the server echo of the submitted effective config for this arm.",
    "- evidence.json: run references, server-derived provenance, and the server export for this arm.",
    "- capsule-live.json: BROWSER-ASSEMBLED from executed faulted-branch evidence (payload and sha256);",
    "  input for real replay tooling, NOT a server-issued capsule.",
    "",
    "Replay (deterministic capsule replay, zero model calls):",
    "  python -m ghostlab replay capsule-live.json",
    "with PYTHONPATH=src. Replay re-checks recorded decisions; it never re-executes.",
    "",
    "Fresh retest (new execution, new ids): re-submit the effective config",
    "from step 6 (Run again) or the retest button on this page.",
    "",
    "No credentials are present in this bundle.",
    "",
  ].join("\n");
  return {
    files: [
      { name: "server-export.json", mediaType: "application/json", content: `${JSON.stringify(server.raw, null, 2)}\n` },
      { name: "effective-config.json", mediaType: "application/json", content: `${JSON.stringify(server.effectiveConfig, null, 2)}\n` },
      { name: "evidence.json", mediaType: "application/json", content: `${JSON.stringify(evidenceDoc, null, 2)}\n` },
      { name: "capsule-live.json", mediaType: "application/json", content: `${JSON.stringify({ payload: evidence.capsule.payload, sha256: evidence.capsule.sha256 }, null, 2)}\n` },
      { name: "README.txt", mediaType: "text/plain", content: readme },
    ],
  };
}

function ServiceExport(props: { ctx: StepContext; evidence: ExperimentEvidence }): ReactNode {
  const { ctx, evidence } = props;
  const arms = evidence.references?.arms ?? [];
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [server, setServer] = useState<ServerExportBundle | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [compareText, setCompareText] = useState<string | null>(null);
  const [compareError, setCompareError] = useState<string | null>(null);
  const [comparing, setComparing] = useState(false);
  const [retesting, setRetesting] = useState(false);
  const [retestError, setRetestError] = useState<string | null>(null);

  const selected: ArmReference | null =
    arms.find((arm) => arm.runId === selectedRunId) ??
    arms.find((arm) => arm.variant === "faulted") ??
    (arms.length > 0 ? (arms[0] as ArmReference) : null);

  const bundle = useMemo(
    () => (selected === null || server === null || server.groupId !== selected.groupId ? null : serviceFiles(evidence, selected, server)),
    [evidence, selected, server],
  );

  if (!isServiceGuide(ctx.api)) return null;
  const api = ctx.api;

  const fetchServerExport = (): void => {
    if (selected === null) return;
    const arm = selected;
    setExporting(true);
    setExportError(null);
    setServer(null);
    api
      .fetchExport(arm.groupId)
      .then((result) => setServer(result))
      .catch((error: unknown) => setExportError(error instanceof Error ? error.message : String(error)))
      .finally(() => setExporting(false));
  };

  const runCompare = (): void => {
    const distinct = [...new Map(arms.map((arm) => [arm.resolvedId, arm])).values()];
    const left = distinct.find((arm) => arm.variant === "baseline") ?? distinct[0];
    const right = distinct.find((arm) => arm.variant === "faulted") ?? distinct[1];
    if (left === undefined || right === undefined || left.resolvedId === right.resolvedId) {
      setCompareText("A single resolved config backs every arm — nothing to compare.");
      return;
    }
    setComparing(true);
    setCompareError(null);
    setCompareText(null);
    api
      .compareResolved(left.resolvedId, right.resolvedId)
      .then((result) => {
        const lines = [
          `left ${left.variant} ${result.leftId} (digest ${result.leftDigest})`,
          `right ${right.variant} ${result.rightId} (digest ${result.rightDigest})`,
          `equal: ${String(result.equal)}; diffs: ${String(result.diffCount)}${result.truncated ? " (truncated)" : ""}`,
          ...result.diffs.map((d) => `${d.path}: ${JSON.stringify(d.left)} -> ${JSON.stringify(d.right)}`),
        ];
        setCompareText(lines.join("\n"));
      })
      .catch((error: unknown) => setCompareError(error instanceof Error ? error.message : String(error)))
      .finally(() => setComparing(false));
  };

  const freshRetest = (): void => {
    setRetesting(true);
    setRetestError(null);
    // The run route cannot start a new attempt for an identical config, so
    // the retest carries the same execution fields with a suffixed objective
    // name (recorded metadata only). New-attempt support is pending.
    const retestConfig: EffectiveConfig = {
      ...(JSON.parse(JSON.stringify(evidence.effectiveConfig)) as EffectiveConfig),
      objective: `${evidence.effectiveConfig.objective} (retest ${new Date().toISOString()})`.slice(0, 2000),
    };
    api
      .runExperiment(retestConfig)
      .then((record) => {
        ctx.replaceConfig(retestConfig);
        ctx.setEvidence(record);
        ctx.notify("Fresh retest executed (new groups, new run ids) — see step 6.");
        ctx.navigate("/guide/6-run");
      })
      .catch((error: unknown) => setRetestError(error instanceof Error ? error.message : String(error)))
      .finally(() => setRetesting(false));
  };

  const readme = bundle?.files.find((f) => f.name === "README.txt") ?? null;
  const evidenceFile = bundle?.files.find((f) => f.name === "evidence.json") ?? null;

  return div(
    null,
    h2(null, "Select the exported run"),
    p(null, "Each arm below is a real execution. The export consumes the selected run's actual recorded artifacts."),
    ...arms.map((arm) =>
      radioRow({
        name: "export-arm",
        id: `export-arm-${arm.runId}`,
        label: `${arm.variant}: run ${arm.runId} (group ${arm.groupId}, ${arm.status}/${arm.verdict}, ${String(arm.toolCalls)} tool calls, ${String(arm.modelCalls)} model calls)`,
        checked: selected !== null && selected.runId === arm.runId,
        onChange: () => {
          setSelectedRunId(arm.runId);
          setServer(null);
          setExportError(null);
        },
        testid: `export-arm-${arm.variant}`,
      }),
    ),
    div(
      { className: "row" },
      button({ onClick: fetchServerExport, label: exporting ? "Fetching…" : "Fetch server export", disabled: exporting || selected === null, testid: "export-fetch" }),
    ),
    exportError !== null ? banner("error", "Export fetch failed", exportError) : null,
    bundle === null
      ? p(null, "No export yet. Fetch the server export for the selected run first — nothing here is pre-fabricated.")
      : div(
          null,
          h2(null, "Export bundle (actual recorded artifacts)"),
          p({ "data-testid": "export-label" }, `Mode ${evidence.mode}; submitted ${evidence.submittedAt}; arm ${selected?.variant ?? "?"}; exported ${server?.exportedAt ?? "?"}.`),
          kvTable([
            ["Group", code(null, server?.groupId ?? "?")],
            ["Resolved", code(null, server?.resolvedId ?? "?")],
            ["Requested", code(null, server?.requestedId ?? "?")],
            ["Status", server?.status ?? "?"],
            ["Resolved digest", code(null, server?.digests.resolvedDigest ?? "?")],
            ["Run digests", (server?.digests.runDigests ?? []).join(", ")],
          ]),
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
        ),
    h2(null, "Compare resolved configs (server-computed)"),
    p(null, "Baseline vs faulted resolved configs, diffed by the service — never in the browser."),
    div(
      { className: "row" },
      button({ onClick: runCompare, label: comparing ? "Comparing…" : "Compare baseline vs faulted", disabled: comparing, testid: "compare-run" }),
    ),
    compareError !== null ? banner("error", "Compare failed", compareError) : null,
    compareText !== null ? codeBlock(compareText, "compare-result") : null,
    h2(null, "Compare totals (executed evidence)"),
    p({ "data-testid": "compare-label" }, evidence.source.experimentResult),
    kvTable([
      ["Baseline total", fmtMinor(evidence.result.baseline.final_state.total_refunded_minor, evidence.result.baseline.final_state.order.currency)],
      ["Faulted total", fmtMinor(evidence.result.faulted.final_state.total_refunded_minor, evidence.result.faulted.final_state.order.currency)],
      ["Repaired total", fmtMinor(evidence.result.repaired.final_state.total_refunded_minor, evidence.result.repaired.final_state.order.currency)],
      ["Violated invariants", evidence.result.diff.violated_invariants.join(", ") || "none"],
    ]),
    divergenceView(evidence.result.diff.first_divergence),
    h2(null, "Replay (deterministic, zero model calls)"),
    p(
      null,
      "Replay re-checks the recorded capsule with the real replay tooling; it never re-executes. ",
      "The capsule input is browser-assembled from executed evidence (see capsule-live.json) — a server-issued capsule is not part of this contract.",
    ),
    codeBlock("python -m ghostlab replay capsule-live.json", "retest-command"),
    h2(null, "Fresh retest (new execution, new ids)"),
    p(null, "Re-submits the recorded config to the service and executes every branch again. This is a new experiment with new run ids — not a replay. The objective name is suffixed (recorded metadata only) because the run route cannot start a new attempt for an identical config; new-attempt support is pending."),
    retestError !== null ? banner("error", "Retest failed", retestError) : null,
    div(
      { className: "row" },
      button({ onClick: freshRetest, label: retesting ? "Retesting…" : "Fresh retest this config", disabled: retesting, testid: "retest-run" }),
    ),
  );
}

function FixtureExport(props: { ctx: StepContext; evidence: ExperimentEvidence }): ReactNode {
  const { ctx, evidence } = props;
  const bundle = useMemo(
    () => (isServiceGuide(ctx.api) ? null : ctx.api.exportBundle(evidence)),
    [ctx.api, evidence],
  );
  if (bundle === null) return null;

  const readme = bundle.files.find((f) => f.name === "README.txt");
  const evidenceFile = bundle.files.find((f) => f.name === "evidence.json");
  const currency = evidence.result.baseline.final_state.order.currency;

  return div(
    null,
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
  );
}

export function Step7(props: { ctx: StepContext }): ReactNode {
  const { ctx } = props;
  const evidence = ctx.evidence;

  if (evidence === null) {
    return div(
      null,
      h1(null, "Step 7: Export, compare & retest"),
      banner("info", "Nothing to export yet", "Run the guided experiment in step 6 first."),
      p(null, navLink(ctx.navigate, "/guide/6-run", "← Back to run")),
    );
  }

  if (!isServiceGuide(ctx.api)) {
    return div(
      null,
      h1(null, "Step 7: Export, compare & retest"),
      banner("ok", "No credentials in this bundle", "Exports carry the synthetic config and evidence only. The acceptance suite scans every export for secret patterns."),
      FixtureExport({ ctx, evidence }),
      p(null, navLink(ctx.navigate, "/guide/6-run", "← Back to run")),
    );
  }

  if (evidence.references === undefined || evidence.references.arms.length === 0) {
    return div(
      null,
      h1(null, "Step 7: Export, compare & retest"),
      banner("warn", "No run references", "This evidence has no captured run references, so no server export can be addressed. Re-run in step 6 to capture references."),
      p(null, navLink(ctx.navigate, "/guide/6-run", "← Back to run")),
    );
  }

  return div(
    null,
    h1(null, "Step 7: Export, compare & retest"),
    banner("ok", "No credentials in this bundle", "Exports carry the synthetic config and evidence only. The acceptance suite scans every export for secret patterns."),
      ServiceExport({ ctx, evidence }),
    p(null, navLink(ctx.navigate, "/guide/6-run", "← Back to run")),
  );
}
