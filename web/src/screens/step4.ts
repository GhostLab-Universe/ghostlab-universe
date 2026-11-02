/* Step 4: configure fixture, faults, invariant focus, limits, objective. */

import { useMemo, type ReactNode } from "react";
import {
  INVARIANT_NAMES,
  validateEffectiveConfig,
  type InvariantName,
} from "../config";
import {
  banner,
  checkRow,
  div,
  errorList,
  field,
  h1,
  h2,
  navLink,
  numberInput,
  p,
  textArea,
  textInput,
  type StepContext,
} from "../ui";

function toggle(list: readonly InvariantName[], name: InvariantName): InvariantName[] {
  return list.includes(name) ? list.filter((n) => n !== name) : [...list, name];
}

export function Step4(props: { ctx: StepContext }): ReactNode {
  const { ctx } = props;
  const config = ctx.config;
  const validation = useMemo(() => validateEffectiveConfig(config), [config]);

  const currency = config.limits.costCurrency ?? "";
  return div(
    null,
    h1(null, "Step 4: Configure fixtures, faults, checks & limits"),
    banner(
      "info",
      "One config, validated twice",
      "Every control below writes one effective-config field. The UI validates it here and the service re-validates the same rules before running.",
    ),
    h2(null, "Fixture (synthetic order)"),
    field(
      "Order ID",
      textInput({
        id: "fixture-order",
        value: config.fixture.orderId,
        onChange: (v) => ctx.update("fixture.orderId", v),
        testid: "fixture-order",
        configPath: "fixture.orderId",
        maxLength: 100,
      }),
      { htmlFor: "fixture-order" },
    ),
    field(
      "Customer ID",
      textInput({
        id: "fixture-customer",
        value: config.fixture.customerId,
        onChange: (v) => ctx.update("fixture.customerId", v),
        testid: "fixture-customer",
        configPath: "fixture.customerId",
        maxLength: 100,
      }),
      { htmlFor: "fixture-customer" },
    ),
    field(
      "Paid amount (minor units, integer)",
      numberInput({
        id: "fixture-paid",
        value: config.fixture.paidMinor,
        allowNull: false,
        onCommit: (v) => {
          if (v !== null) ctx.update("fixture.paidMinor", v);
        },
        testid: "fixture-paid",
        configPath: "fixture.paidMinor",
      }),
      { hint: "INR minor units. 250000 = INR 2,500.00. Integers only.", htmlFor: "fixture-paid" },
    ),
    field(
      "Currency",
      textInput({
        id: "fixture-currency",
        value: config.fixture.currency,
        onChange: (v) => ctx.update("fixture.currency", v),
        testid: "fixture-currency",
        configPath: "fixture.currency",
        maxLength: 100,
      }),
      { htmlFor: "fixture-currency" },
    ),
    h2(null, "Faults"),
    checkRow({
      id: "fault-timeout",
      label: "Timeout after commit: the refund commits, then the response times out.",
      checked: config.faults.timeoutAfterCommit,
      onChange: (v) => ctx.update("faults.timeoutAfterCommit", v),
      testid: "fault-timeout",
      configPath: "faults.timeoutAfterCommit",
    }),
    h2(null, "Invariant focus"),
    p(null, "Which verifier invariants this run highlights. Verdicts always come from executed evidence, never from this selection."),
    ...INVARIANT_NAMES.map((name) =>
      checkRow({
        id: `check-${name}`,
        label: name,
        checked: config.checks.invariants.includes(name),
        onChange: () => ctx.update("checks.invariants", toggle(config.checks.invariants, name)),
        testid: `check-${name}`,
        configPath: "checks.invariants",
      }),
    ),
    h2(null, "Limits (ceilings enforced by the service)"),
    field(
      "Max tool calls (1..1000)",
      numberInput({
        id: "limit-tools",
        value: config.limits.maxToolCalls,
        allowNull: false,
        onCommit: (v) => {
          if (v !== null) ctx.update("limits.maxToolCalls", v);
        },
        testid: "limit-tools",
        configPath: "limits.maxToolCalls",
      }),
      { htmlFor: "limit-tools" },
    ),
    field(
      "Max model calls",
      numberInput({
        id: "limit-models",
        value: config.limits.maxModelCalls,
        allowNull: false,
        onCommit: (v) => {
          if (v !== null) ctx.update("limits.maxModelCalls", v);
        },
        testid: "limit-models",
        configPath: "limits.maxModelCalls",
      }),
      { htmlFor: "limit-models" },
    ),
    field(
      "Max input tokens",
      numberInput({
        id: "limit-input",
        value: config.limits.maxInputTokens,
        allowNull: false,
        onCommit: (v) => {
          if (v !== null) ctx.update("limits.maxInputTokens", v);
        },
        testid: "limit-input",
        configPath: "limits.maxInputTokens",
      }),
      { htmlFor: "limit-input" },
    ),
    field(
      "Max output tokens",
      numberInput({
        id: "limit-output",
        value: config.limits.maxOutputTokens,
        allowNull: false,
        onCommit: (v) => {
          if (v !== null) ctx.update("limits.maxOutputTokens", v);
        },
        testid: "limit-output",
        configPath: "limits.maxOutputTokens",
      }),
      { htmlFor: "limit-output" },
    ),
    field(
      "Max wall clock (ms)",
      numberInput({
        id: "limit-clock",
        value: config.limits.maxWallClockMs,
        allowNull: false,
        onCommit: (v) => {
          if (v !== null) ctx.update("limits.maxWallClockMs", v);
        },
        testid: "limit-clock",
        configPath: "limits.maxWallClockMs",
      }),
      { htmlFor: "limit-clock" },
    ),
    field(
      "Max parallel runs (1..64)",
      numberInput({
        id: "limit-parallel",
        value: config.limits.maxParallelRuns,
        allowNull: false,
        onCommit: (v) => {
          if (v !== null) ctx.update("limits.maxParallelRuns", v);
        },
        testid: "limit-parallel",
        configPath: "limits.maxParallelRuns",
      }),
      { htmlFor: "limit-parallel" },
    ),
    field(
      "Max cost (minor units, empty = none)",
      numberInput({
        id: "limit-cost",
        value: config.limits.maxCostMinor,
        allowNull: true,
        onCommit: (v) => ctx.update("limits.maxCostMinor", v),
        testid: "limit-cost",
        configPath: "limits.maxCostMinor",
      }),
      { htmlFor: "limit-cost" },
    ),
    field(
      "Cost currency (empty = none; set together with max cost)",
      textInput({
        id: "limit-currency",
        value: currency,
        onChange: (v) => ctx.update("limits.costCurrency", v === "" ? null : v),
        testid: "limit-currency",
        configPath: "limits.costCurrency",
        maxLength: 3,
        placeholder: "USD",
      }),
      { htmlFor: "limit-currency" },
    ),
    h2(null, "Objective"),
    field(
      "Objective",
      textInput({
        id: "objective",
        value: config.objective,
        onChange: (v) => ctx.update("objective", v),
        testid: "objective",
        configPath: "objective",
        maxLength: 2000,
      }),
      { htmlFor: "objective" },
    ),
    field(
      "Success criteria (one per line)",
      textArea({
        id: "criteria",
        rows: 3,
        value: config.successCriteria.join("\n"),
        onChange: (v) =>
          ctx.update(
            "successCriteria",
            v
              .split("\n")
              .map((line) => line.trim())
              .filter((line) => line !== ""),
          ),
        testid: "criteria",
        configPath: "successCriteria",
      }),
      { htmlFor: "criteria" },
    ),
    validation.ok ? null : errorList(validation.errors),
    p(null, navLink(ctx.navigate, "/guide/5-review", "Continue to review →")),
  );
}
