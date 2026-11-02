/* Step 3: select the target agent adapter. */

import type { ReactNode } from "react";
import { ADAPTER_VERSION } from "../config";
import {
  banner,
  div,
  field,
  h1,
  h2,
  kvTable,
  navLink,
  p,
  radioRow,
  textArea,
  type StepContext,
} from "../ui";

export function Step3(props: { ctx: StepContext }): ReactNode {
  const { ctx } = props;
  return div(
    null,
    h1(null, "Step 3: Select agent"),
    banner(
      "info",
      "Scripted inference in fixture mode",
      "Fixture mode shows approved results instead of executing. The live core runs deterministic adapters; a model-driven adapter is a separate experiment.",
    ),
    h2(null, "Adapter"),
    radioRow({
      name: "adapter",
      id: "adapter-vulnerable",
      label: "Vulnerable: retries a timed-out refund with a fresh idempotency key (can duplicate the refund).",
      checked: ctx.config.agent.adapter === "vulnerable",
      onChange: () => ctx.update("agent.adapter", "vulnerable"),
      testid: "agent-vulnerable",
      configPath: "agent.adapter",
    }),
    radioRow({
      name: "adapter",
      id: "adapter-repaired",
      label: "Repaired: checks refund status before retrying, reusing the original idempotency key.",
      checked: ctx.config.agent.adapter === "repaired",
      onChange: () => ctx.update("agent.adapter", "repaired"),
      testid: "agent-repaired",
      configPath: "agent.adapter",
    }),
    kvTable([["Adapter version", ADAPTER_VERSION]]),
    field(
      "Operator note (recorded with the run)",
      textArea({
        id: "agent-note",
        rows: 3,
        value: ctx.config.agent.note,
        onChange: (value) => ctx.update("agent.note", value),
        testid: "agent-note",
        configPath: "agent.note",
        maxLength: 2000,
      }),
      { hint: "Why this adapter for this run. Stored in the evidence; never sent to a model.", htmlFor: "agent-note" },
    ),
    p(null, navLink(ctx.navigate, "/guide/4-configure", "Continue to configuration →")),
  );
}
