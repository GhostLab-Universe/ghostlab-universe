/* Step 2: inspect capability discovery, qualification and env limits. */

import { useEffect, useState, type ReactNode } from "react";
import { ServiceGuideError, isServiceGuide } from "../serviceClient";
import type { CapabilitiesView, QualificationView } from "../types";
import {
  banner,
  button,
  code,
  div,
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

export function Step2(props: { ctx: StepContext }): ReactNode {
  const { ctx } = props;
  const [caps, setCaps] = useState<CapabilitiesView | null>(null);
  const [report, setReport] = useState<QualificationView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reportMissing, setReportMissing] = useState(false);
  const [qualifying, setQualifying] = useState(false);
  const [qualifyError, setQualifyError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const envId = ctx.config.env.envId;
  const digest = ctx.config.env.manifestDigest;
  const service = isServiceGuide(ctx.api);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    setReportMissing(false);
    setCaps(null);
    setReport(null);
    ctx.api
      .getCapabilities(envId, digest)
      .then((c) => {
        if (!cancelled) setCaps(c);
      })
      .catch((failure: unknown) => {
        if (!cancelled) setError(failure instanceof Error ? failure.message : String(failure));
      });
    ctx.api
      .getQualification(envId)
      .then((r) => {
        if (!cancelled) setReport(r);
      })
      .catch((failure: unknown) => {
        if (cancelled) return;
        if (failure instanceof ServiceGuideError && failure.kind === "not-found") {
          setReportMissing(true);
          return;
        }
        setError(failure instanceof Error ? failure.message : String(failure));
      });
    return () => {
      cancelled = true;
    };
  }, [ctx.api, envId, digest, reloadToken]);

  const runQualification = (): void => {
    if (!isServiceGuide(ctx.api)) return;
    const api = ctx.api;
    setQualifying(true);
    setQualifyError(null);
    api
      .qualifyEnv(envId, digest)
      .then((r) => {
        setReport(r);
        setReportMissing(false);
        ctx.notify(`Qualification ${r.status} for ${r.envId}.`);
      })
      .catch((failure: unknown) => {
        setQualifyError(failure instanceof Error ? failure.message : String(failure));
      })
      .finally(() => setQualifying(false));
  };

  const reportPanel =
    report !== null
      ? div(
          null,
          h2(null, "Qualification report"),
          p({ "data-testid": "qualification-label" }, report.label),
          kvTable([
            ["Status", report.status],
            ["Runner", report.runnerVersion],
            ["Created", report.createdAt],
          ]),
          table(
            { className: "checks", "data-testid": "qualification-table" },
            thead(null, tr(null, th(null, "Check"), th(null, "Passed"), th(null, "Detail"))),
            tbody(
              null,
              ...report.checks.map((check) =>
                tr({ key: check.name }, td(null, code(null, check.name)), td(null, check.passed ? "passed" : "FAILED"), td(null, check.detail)),
              ),
            ),
          ),
        )
      : reportMissing && service
        ? div(
            null,
            h2(null, "Qualification report"),
            banner("warn", "No qualification report", "This env has no report in the registry. Qualification executes the env on purpose and binds the outcome to the exact registered code and manifest."),
            qualifyError !== null
              ? div(null, banner("error", "Qualification failed", qualifyError), p({ "data-testid": "qualify-error" }, qualifyError))
              : null,
            button({
              onClick: runQualification,
              label: qualifying ? "Qualifying…" : "Run qualification (executes the env)",
              disabled: qualifying,
              testid: "qualify-run",
            }),
          )
        : null;

  return div(
    null,
    h1(null, "Step 2: Inspect qualification & limits"),
    service
      ? banner("info", "Executed env data", "Capability discovery and qualification below come from the service for the selected env — never dev aids.")
      : banner("warn", "Illustrative env data", "Capability and qualification panels below are dev aids, not verdicts about any real environment."),
    error !== null ? banner("error", "Could not load environment data", error) : null,
    error !== null && service
      ? button({ onClick: () => setReloadToken((n) => n + 1), label: "Retry", testid: "envs-retry" })
      : null,
    caps === null && report === null && !reportMissing
      ? p(null, "Loading environment data…")
      : div(
          null,
          caps === null
            ? null
            : div(
                null,
                h2(null, "Capability discovery"),
                p({ "data-testid": "capabilities-label" }, caps.label),
                kvTable([
                  ["Environment", `${caps.envId} (${caps.envVersion})`],
                  ["Adapter", code(null, caps.adapterRef)],
                  ["Manifest digest", caps.manifestDigest ?? "none imported"],
                  ["Declared vs discovered", caps.match ? "match" : `MISMATCH: ${caps.mismatches.join(", ")}`],
                  ["Fault hooks (limits of what can be armed)", caps.faultHooks.join(", ") || "none"],
                  ["Reproduction modes", caps.discoveredModes.join(", ")],
                  ["Cancellation", caps.supportsCancellation ? "supported" : "NOT supported"],
                  ["Isolation", caps.isolation],
                ]),
                h2(null, "Declared tools"),
                table(
                  { className: "trace", "data-testid": "tools-table" },
                  thead(null, tr(null, th(null, "Tool"), th(null, "Parameters"))),
                  tbody(
                    null,
                    ...caps.discoveredTools.map((tool) =>
                      tr({ key: tool.name }, td(null, code(null, tool.name)), td(null, tool.parameters.join(", "))),
                    ),
                  ),
                ),
              ),
          reportPanel,
        ),
    p(null, "Run budgets (tool/model/token/wall-clock ceilings) are configured in step 4 and enforced by the service, not the browser."),
    p(null, navLink(ctx.navigate, "/guide/3-agent", "Continue to agent selection →")),
  );
}
