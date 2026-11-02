/* Tiny React helpers for the Track D guided UI. Plain .ts + createElement:
 * no JSX transform, no extra dependencies. State lives in App; screens are
 * components so per-screen hooks stay mounted per route. */

import { createElement, useState, type ReactNode } from "react";
import type {
  Divergence,
  LedgerRow,
  RefundChecks,
  TraceEvent,
} from "../../schemas/typescript/contracts";
import type { ConfigError, EffectiveConfig, InvariantName } from "./config";
import type { ExperimentApi } from "./api";
import type { RecoveryOutcome, ServiceGuideApi } from "./serviceClient";
import type { ExperimentEvidence } from "./types";

export type Props = Record<string, unknown>;
export type Child = ReactNode;

export function h(tag: string, props?: Props | null, ...children: Child[]): ReactNode {
  return createElement(tag, props ?? null, ...children);
}

function tag(name: string): (props?: Props | null, ...children: Child[]) => ReactNode {
  return (props, ...children) => h(name, props ?? null, ...children);
}

export const div = tag("div");
export const span = tag("span");
export const p = tag("p");
export const h1 = tag("h1");
export const h2 = tag("h2");
export const h3 = tag("h3");
export const ul = tag("ul");
export const li = tag("li");
export const nav = tag("nav");
export const main = tag("main");
export const section = tag("section");
export const header = tag("header");
export const footer = tag("footer");
export const code = tag("code");
export const pre = tag("pre");
export const strong = tag("strong");
export const em = tag("em");
export const small = tag("small");
export const table = tag("table");
export const thead = tag("thead");
export const tbody = tag("tbody");
export const tr = tag("tr");
export const th = tag("th");
export const td = tag("td");
export const fieldset = tag("fieldset");
export const legend = tag("legend");

export type ChangeEventLike = { target: { value: string; checked: boolean } };
export type MouseEventLike = { preventDefault: () => void };

export function button(
  props: Props & { onClick: (event: MouseEventLike) => void; label: string; disabled?: boolean; testid?: string },
): ReactNode {
  const out: Props = { type: "button", onClick: props.onClick, className: "btn" };
  if (props.disabled === true) out["disabled"] = true;
  if (typeof props.testid === "string") out["data-testid"] = props.testid;
  if (typeof props.className === "string") out["className"] = `btn ${props.className}`;
  return h("button", out, props.label);
}

export function navLink(
  navigate: (path: string) => void,
  href: string,
  label: string,
  current?: boolean,
): ReactNode {
  const out: Props = {
    href,
    onClick: (event: MouseEventLike) => {
      event.preventDefault();
      navigate(href);
    },
  };
  if (current === true) out["aria-current"] = "page";
  return h("a", out, label);
}

/** Integer-only money: "250000 minor (INR 2,500.00)". Display only. */
export function fmtMinor(minor: number, currency: string): string {
  const sign = minor < 0 ? "-" : "";
  const abs = Math.abs(minor);
  const major = Math.floor(abs / 100);
  const cents = abs % 100;
  const grouped = major.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const centsText = cents < 10 ? `0${cents}` : `${cents}`;
  return `${minor.toLocaleString("en-US")} minor (${sign}${currency} ${grouped}.${centsText})`;
}

export function banner(kind: "info" | "warn" | "error" | "ok", title: string, body?: Child): ReactNode {
  return div(
    { className: `banner banner-${kind}`, role: kind === "error" ? "alert" : "status" },
    strong(null, title),
    body === undefined ? null : p(null, body),
  );
}

export function field(
  labelText: string,
  control: Child,
  opts?: { hint?: string; error?: string | null; htmlFor?: string },
): ReactNode {
  const labelProps: Props = {};
  if (opts?.htmlFor !== undefined) labelProps["htmlFor"] = opts.htmlFor;
  return div(
    { className: "field" },
    h("label", labelProps, labelText),
    control,
    opts?.hint === undefined ? null : small({ className: "hint" }, opts.hint),
    opts?.error === undefined || opts.error === null ? null : small({ className: "field-error", role: "alert" }, opts.error),
  );
}

export interface TextInputOpts {
  id: string;
  value: string;
  onChange: (value: string) => void;
  testid: string;
  configPath?: string;
  maxLength?: number;
  placeholder?: string;
  disabled?: boolean;
}

export function textInput(opts: TextInputOpts): ReactNode {
  const out: Props = {
    id: opts.id,
    type: "text",
    value: opts.value,
    "data-testid": opts.testid,
    onChange: (event: ChangeEventLike) => opts.onChange(event.target.value),
  };
  if (opts.configPath !== undefined) out["data-config-path"] = opts.configPath;
  if (opts.maxLength !== undefined) out["maxLength"] = opts.maxLength;
  if (opts.placeholder !== undefined) out["placeholder"] = opts.placeholder;
  if (opts.disabled === true) out["disabled"] = true;
  return h("input", out);
}

export interface TextAreaOpts extends TextInputOpts {
  rows: number;
}

export function textArea(opts: TextAreaOpts): ReactNode {
  const out: Props = {
    id: opts.id,
    rows: opts.rows,
    value: opts.value,
    "data-testid": opts.testid,
    onChange: (event: ChangeEventLike) => opts.onChange(event.target.value),
  };
  if (opts.configPath !== undefined) out["data-config-path"] = opts.configPath;
  if (opts.maxLength !== undefined) out["maxLength"] = opts.maxLength;
  if (opts.disabled === true) out["disabled"] = true;
  return h("textarea", out);
}

/** Numeric input with a local draft; commits only valid integers. */
export function numberInput(opts: {
  id: string;
  value: number | null;
  allowNull: boolean;
  onCommit: (value: number | null) => void;
  testid: string;
  configPath: string;
  disabled?: boolean;
}): ReactNode {
  const committed = opts.value === null ? "" : String(opts.value);
  const [draft, setDraft] = useState(committed);
  const [error, setError] = useState<string | null>(null);
  const shown = draft;
  if (draft !== committed && error === null && draft === "") {
    // Parent reset (e.g. step revisit): resync the empty draft.
    setDraft(committed);
  }
  const commit = (raw: string): void => {
    setDraft(raw);
    const trimmed = raw.trim();
    if (trimmed === "") {
      if (opts.allowNull) {
        setError(null);
        opts.onCommit(null);
      } else {
        setError("Enter an integer.");
      }
      return;
    }
    if (!/^-?\d+$/.test(trimmed)) {
      setError("Enter an integer (no decimals, no text).");
      return;
    }
    const parsed = Number(trimmed);
    if (!Number.isSafeInteger(parsed)) {
      setError("Integer out of safe range.");
      return;
    }
    setError(null);
    opts.onCommit(parsed);
  };
  const inputProps: Props = {
    id: opts.id,
    type: "text",
    inputMode: "numeric",
    value: shown,
    "data-testid": opts.testid,
    "data-config-path": opts.configPath,
    onChange: (event: ChangeEventLike) => commit(event.target.value),
  };
  if (opts.disabled === true) inputProps["disabled"] = true;
  return div(
    null,
    h("input", inputProps),
    error === null ? null : small({ className: "field-error", role: "alert" }, error),
  );
}

export function checkRow(opts: {
  id: string;
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  testid: string;
  configPath?: string;
}): ReactNode {
  const inputProps: Props = {
    id: opts.id,
    type: "checkbox",
    checked: opts.checked,
    "data-testid": opts.testid,
    onChange: (event: ChangeEventLike) => opts.onChange(event.target.checked),
  };
  if (opts.configPath !== undefined) inputProps["data-config-path"] = opts.configPath;
  return div({ className: "check-row" }, h("input", inputProps), h("label", { htmlFor: opts.id }, opts.label));
}

export function radioRow(opts: {
  name: string;
  id: string;
  label: string;
  checked: boolean;
  onChange: () => void;
  testid: string;
  configPath?: string;
}): ReactNode {
  const inputProps: Props = {
    name: opts.name,
    id: opts.id,
    type: "radio",
    checked: opts.checked,
    "data-testid": opts.testid,
    onChange: () => opts.onChange(),
  };
  if (opts.configPath !== undefined) inputProps["data-config-path"] = opts.configPath;
  return div({ className: "check-row" }, h("input", inputProps), h("label", { htmlFor: opts.id }, opts.label));
}

export function kvTable(rows: ReadonlyArray<readonly [string, Child]>): ReactNode {
  return table(
    { className: "kv" },
    tbody(
      null,
      ...rows.map(([key, value], index) => tr({ key: `${index}-${key}` }, th(null, key), td(null, value))),
    ),
  );
}

export function errorList(errors: readonly ConfigError[]): ReactNode {
  if (errors.length === 0) return null;
  return div(
    { className: "banner banner-error", role: "alert", "data-testid": "config-errors" },
    strong(null, `${errors.length} config problem${errors.length === 1 ? "" : "s"} to fix before running:`),
    ul(null, ...errors.map((err, index) => li({ key: index }, code(null, err.path), ` ${err.message}`))),
  );
}

export function codeBlock(text: string, testid: string): ReactNode {
  return pre({ className: "codeblock", "data-testid": testid }, code(null, text));
}

export function checksTable(checks: RefundChecks, focus: readonly InvariantName[]): ReactNode {
  const rows: ReadonlyArray<readonly [InvariantName, boolean]> = [
    ["authorized_refunds", checks.authorized_refunds],
    ["total_refund_within_payment", checks.total_refund_within_payment],
    ["no_duplicate_full_refund", checks.no_duplicate_full_refund],
    ["unique_idempotency_keys", checks.unique_idempotency_keys],
  ];
  return table(
    { className: "checks", "data-testid": "checks-table" },
    thead(null, tr(null, th(null, "Invariant"), th(null, "Verdict"), th(null, "In focus"))),
    tbody(
      null,
      ...rows.map(([name, passed]) =>
        tr({ key: name }, td(null, code(null, name)), td(null, passed ? "passed" : "FAILED"), td(null, focus.includes(name) ? "yes" : "no")),
      ),
    ),
  );
}

function outcomeText(outcome: TraceEvent["outcome"]): string {
  if ("error" in outcome) return `${outcome.error}: ${outcome.message}`;
  return JSON.stringify(outcome.result);
}

export function traceTable(trace: readonly TraceEvent[], testid: string): ReactNode {
  return table(
    { className: "trace", "data-testid": testid },
    thead(null, tr(null, th(null, "Event"), th(null, "Tool"), th(null, "Outcome"), th(null, "Ledger total (minor)"))),
    tbody(
      null,
      ...trace.map((event) =>
        tr(
          { key: event.event_id },
          td(null, String(event.event_id)),
          td(null, code(null, event.request.tool)),
          td(null, code(null, outcomeText(event.outcome))),
          td(null, String(event.state.total_refunded_minor)),
        ),
      ),
    ),
  );
}

export function ledgerTable(ledger: readonly LedgerRow[]): ReactNode {
  if (ledger.length === 0) return p(null, "Ledger is empty.");
  return table(
    { className: "trace" },
    thead(null, tr(null, th(null, "Transaction"), th(null, "Amount (minor)"), th(null, "Idempotency key"))),
    tbody(
      null,
      ...ledger.map((row) =>
        tr(
          { key: row.transaction_id },
          td(null, code(null, row.transaction_id)),
          td(null, String(row.amount_minor)),
          td(null, code(null, row.idempotency_key)),
        ),
      ),
    ),
  );
}

function divergenceCell(event: TraceEvent | null): Child {
  if (event === null) return "absent";
  return `${event.request.tool} #${event.event_id}: ${outcomeText(event.outcome)}`;
}

export function divergenceView(first: Divergence | null): ReactNode {
  if (first === null) return p({ "data-testid": "divergence-none" }, "No divergence: traces match.");
  return div(
    { "data-testid": "divergence", className: "divergence" },
    h2(null, `First divergence at event ${first.event_id}`),
    kvTable([
      ["Baseline", divergenceCell(first.baseline)],
      ["Faulted", divergenceCell(first.faulted)],
    ]),
  );
}

export function stepper(
  steps: ReadonlyArray<{ path: string; title: string }>,
  currentPath: string,
  navigate: (path: string) => void,
): ReactNode {
  return nav(
    { className: "stepper", "aria-label": "Guided run steps" },
    ul(
      null,
      ...steps.map((step, index) => {
        const current = step.path === currentPath;
        return li(
          { key: step.path },
          navLink(navigate, step.path, `${index + 1}. ${step.title}`, current),
        );
      }),
    ),
  );
}

/** Shared per-screen context: one config, one evidence record, one API. */
export interface ServiceInfo {
  baseUrl: string;
  projectId: string;
  projectObserved: boolean;
}

export interface StepContext {
  config: EffectiveConfig;
  /** Service adapter in production; fixture only via the dev-only flag. */
  api: ExperimentApi | ServiceGuideApi;
  evidence: ExperimentEvidence | null;
  setEvidence: (next: ExperimentEvidence | null) => void;
  manifestText: string;
  setManifestText: (next: string) => void;
  importDigest: string | null;
  importCanonical: string | null;
  setImport: (digest: string | null, canonical: string | null) => void;
  update: (path: string, value: unknown) => void;
  /** Replace the whole config (recovery restore, retest input). */
  replaceConfig: (next: EffectiveConfig) => void;
  notices: readonly string[];
  notify: (message: string) => void;
  navigate: (path: string) => void;
  /** Service connection facts; null on the fixture path. */
  service: ServiceInfo | null;
  /** Reload-recovery outcomes (service only); empty on the fixture path. */
  recovery: readonly RecoveryOutcome[];
  refreshRecovery: () => void;
}
