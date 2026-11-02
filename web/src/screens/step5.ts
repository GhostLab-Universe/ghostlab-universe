/* Step 5: review the effective config before running. */

import { useMemo, type ReactNode } from "react";
import {
  CONTROL_PATHS,
  flattenConfig,
  toCampaignSpec,
  toExperimentProposal,
  validateEffectiveConfig,
} from "../config";
import {
  banner,
  code,
  codeBlock,
  div,
  errorList,
  h1,
  h2,
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

export function Step5(props: { ctx: StepContext }): ReactNode {
  const { ctx } = props;
  const rows = useMemo(() => flattenConfig(ctx.config), [ctx.config]);
  const validation = useMemo(() => validateEffectiveConfig(ctx.config), [ctx.config]);
  const campaign = useMemo(() => toCampaignSpec(ctx.config), [ctx.config]);
  const proposal = useMemo(() => toExperimentProposal(ctx.config), [ctx.config]);

  return div(
    null,
    h1(null, "Step 5: Review effective config"),
    banner(
      "info",
      "Same config the run records",
      "Every control from steps 1–4 lands in exactly one row below. The run request carries this JSON, and the evidence echoes it back.",
    ),
    validation.ok
      ? banner("ok", "Config valid", "It satisfies the UI checks; the service re-validates the same rules.")
      : errorList(validation.errors),
    h2(null, "Field provenance"),
    table(
      { className: "trace", "data-testid": "review-table" },
      thead(null, tr(null, th(null, "Step"), th(null, "Field"), th(null, "Path"), th(null, "Value"))),
      tbody(
        null,
        ...rows.map(([path, value], index) => {
          const control = CONTROL_PATHS[index];
          const step = control === undefined ? "?" : String(control.step);
          const label = control === undefined ? path : control.label;
          return tr(
            { key: path },
            td(null, step),
            td(null, label),
            td(null, code(null, path)),
            td(null, code(null, JSON.stringify(value) ?? "null")),
          );
        }),
      ),
    ),
    h2(null, "Effective config JSON"),
    codeBlock(JSON.stringify(ctx.config, null, 2), "effective-config"),
    h2(null, "Service payloads derived from this config"),
    p(null, "CampaignSpec (volume/budget envelope for the run request):"),
    codeBlock(JSON.stringify(campaign, null, 2), "campaign-spec"),
    p(null, "ExperimentProposal (deterministic text derived from the config; no model output):"),
    codeBlock(JSON.stringify(proposal, null, 2), "experiment-proposal"),
    p(null, navLink(ctx.navigate, "/guide/6-run", "Continue to run →")),
  );
}
