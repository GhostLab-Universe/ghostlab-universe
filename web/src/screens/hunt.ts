/* Step 6 story views on the live-model routes (docs/api.md): Hunt → Failure →
 * Diagnosis → Fix & retest → Capsule replay → Evidence. Every number shown
 * comes from the service; verdicts come only from the controller's
 * deterministic checks. Model text is labelled as explanation, never evidence. */

import { createElement, useEffect, useState, type ReactNode } from "react";
import {
  button,
  code,
  div,
  h,
  h2,
  h3,
  li,
  p,
  section,
  span,
  strong,
  table,
  tbody,
  td,
  th,
  thead,
  tr,
  ul,
  type Child,
} from "../ui";

// ---- wire shapes (subset of docs/api.md) ---------------------------------

interface ModelCall {
  model?: string | null;
  provider?: string | null;
  latency_ms?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  usd?: number | null;
  replayed?: boolean;
  role?: string;
  request_id?: string | null;
}
interface Fault {
  hook: string;
  trigger?: { type?: string; tool?: string; occurrence?: number };
  fired?: boolean;
}
interface Experiment {
  run_id: string | null;
  agent: string;
  faults: Fault[];
  outcome: string;
  reason: string | null;
  verdict: string | null;
  rules_failed: string[];
  model_calls?: number;
  tool_calls?: number;
  hypothesis?: string | null;
  proposed_by?: ModelCall;
}
interface Budget {
  usd_spent: number;
  usd_cap: number;
  usd_target_agent?: number;
  experiments: number;
  max_experiments: number;
}
interface Minimized {
  required_prefix?: string[];
  dropped?: number;
  stable?: boolean;
  actions?: Array<{ tool: string; arguments: unknown }>;
}
interface Diagnosis {
  label: string;
  model?: string | null;
  root_cause?: string | null;
  proposed_patch?: string | null;
  unparsed?: string;
  rerun?: { agent: string; capsule?: string; run_id?: string; verdict?: string; rules_failed?: string[]; evidence?: string };
}
interface Exploration {
  exploration_id: string;
  mode: string;
  provider: string;
  status: string;
  stop_status?: string | null;
  recording?: string | null;
  experiments?: Experiment[];
  budget?: Budget;
  recorded_budget?: Budget | null;
  model_calls?: ModelCall[];
  failure?: (Experiment & { minimized?: Minimized }) | null;
  diagnosis?: Diagnosis | null;
  reproduced?: boolean | null;
  final_text?: string | null;
  error?: string | null;
}
interface LedgerRow {
  transaction_id: string;
  amount_minor: number;
  idempotency_key: string;
}
interface TraceRow {
  event_id: number;
  tool: string;
  arguments: unknown;
  outcome: { result?: unknown; error?: string; message?: string };
}
interface Branch {
  run_id: string;
  verdict: string;
  ledger: LedgerRow[];
  order?: { order_id: string; paid_minor: number; currency: string } | null;
  total_refunded_minor: number;
  trace: TraceRow[];
}
interface Claim {
  verdict: string;
  claim: { refunds_issued: number; total_refunded: number } | null;
  actual: { refunds_issued: number; total_refunded_minor: number };
}
interface RunDetail {
  run_id: string;
  agent: string;
  faults: Fault[];
  baseline: Branch | null;
  faulted: Branch;
  first_divergence_event_id: number | null;
  violated_invariants: string[];
  claim_check: Claim | null;
}
interface ReplayOut {
  name: string;
  kind: string;
  reproduced: boolean;
  matches: Record<string, unknown>;
  verdict?: string;
  claim_check?: Claim;
  duplicate_refund?: boolean;
  model_calls_replayed?: number;
  model_calls_live: number;
  summary?: { faults?: Fault[]; verdict?: string; rules_failed?: string[]; agent?: string } & Record<string, unknown>;
}
interface EvidenceOut {
  calibration: {
    generated_at: string;
    model: string;
    provider: string;
    fault: { hook: string };
    usd_spent: number;
    model_calls: number;
    table: Array<Record<string, string | number>>;
  } | null;
  calibration_notes: string | null;
  benchmark: {
    generated_at: string;
    budget: number;
    space_cells: number;
    failing_cells: unknown[];
    strategies: Record<string, { trials: number; discovered: number; mean_experiments_to_first_failure: number | null; max_experiments_to_first_failure: number | null; usd_total: number } | null>;
  } | null;
  benchmark_caveats: string | null;
}

// ---- helpers --------------------------------------------------------------

async function call<T>(base: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = body === undefined ? {} : { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(`${base}${path}`, init);
  const data = (await res.json().catch(() => null)) as (T & { error?: string; detail?: string }) | null;
  if (!res.ok || data === null) {
    throw new Error(data?.detail ?? data?.error ?? `HTTP ${String(res.status)} on ${path}`);
  }
  return data;
}

function errText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** "₹2,500" from integer minor units (display only). */
export function inr(minor: number): string {
  const major = minor / 100;
  return `₹${major.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

function usd(value: number | null | undefined, digits = 4): string {
  return value === null || value === undefined ? "—" : `$${value.toFixed(digits)}`;
}

function plural(n: number, word: string): string {
  return `${String(n)} ${word}${n === 1 ? "" : "s"}`;
}

function badge(verdict: string | null, outcome?: string): ReactNode {
  const executed = outcome === undefined || outcome === "executed";
  const label = !executed || verdict === null || verdict === "incomplete" ? "UNTESTED" : verdict.toUpperCase();
  const kind = label === "PASS" ? "pass" : label === "FAIL" ? "fail" : "untested";
  return span({ className: `badge badge-${kind}`, "data-testid": "verdict-badge" }, label);
}

function faultText(f: Fault): string {
  const t = f.trigger ?? {};
  return `${f.hook} · on ${t.tool ?? "*"} #${String(t.occurrence ?? "?")}`;
}

function spinner(label: string): ReactNode {
  return div({ className: "spinner-row", role: "status" }, span({ className: "spinner", "aria-hidden": "true" }), span(null, label));
}

function stat(label: string, value: Child, testid?: string): ReactNode {
  return div({ className: "stat", "data-testid": testid ?? null }, span({ className: "stat-value" }, value), span({ className: "stat-label" }, label));
}

/** Tiny markdown subset for caveat text from the API: bullets, **bold**, `code`. */
function inline(text: string): Child[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter((s) => s !== "").map((part, i) => {
    if (part.startsWith("**")) return strong({ key: i }, part.slice(2, -2).replace(/`/g, ""));
    if (part.startsWith("`")) return code({ key: i }, part.slice(1, -1));
    return part;
  });
}

function markdown(text: string | null): ReactNode {
  if (text === null) return p({ className: "muted" }, "No notes recorded.");
  const items = text.split("\n").filter((l) => l.trim() !== "");
  return ul(
    { className: "md" },
    ...items.map((line, i) => {
      const nested = /^\s{2,}-/.test(line);
      const body = line.replace(/^\s*-\s*/, "");
      return li({ key: i, className: nested ? "md-nested" : undefined }, ...inline(body));
    }),
  );
}

// ---- views ----------------------------------------------------------------

type View = "hunt" | "failure" | "diagnosis" | "fix" | "replay" | "evidence";
const VIEWS: ReadonlyArray<readonly [View, string]> = [
  ["hunt", "Hunt"],
  ["failure", "Failure"],
  ["diagnosis", "Diagnosis"],
  ["fix", "Fix & retest"],
  ["replay", "Capsule replay"],
  ["evidence", "Evidence"],
];

const STOP_TEXT: Record<string, string> = {
  found_failure: "found failure",
  coverage_complete: "coverage complete",
  budget_exhausted: "budget exhausted",
  paused_on_error: "paused on error",
  model_stopped: "model stopped",
};

function stopChip(exp: Exploration): ReactNode {
  if (exp.status === "running") return span({ className: "chip chip-running", "data-testid": "stop-chip" }, "running");
  const stop = exp.stop_status ?? "stopped";
  const kind = stop === "found_failure" ? "fail" : stop === "paused_on_error" ? "warn" : "neutral";
  return span({ className: `chip chip-${kind}`, "data-testid": "stop-chip" }, STOP_TEXT[stop] ?? stop);
}

function budgetMeter(exp: Exploration): ReactNode {
  const b = exp.budget;
  if (b === undefined) return null;
  const recorded = exp.recorded_budget ?? null;
  const pct = Math.min(100, (b.usd_spent / b.usd_cap) * 100);
  return div(
    { className: "meter", "data-testid": "budget-meter" },
    stat(
      exp.mode === "replay" ? "USD spent by this replay / cap" : "USD spent / cap",
      [usd(b.usd_spent), span({ key: "c", className: "stat-cap" }, ` / ${usd(b.usd_cap, 2)}`)],
      "budget-usd",
    ),
    stat("Experiments / max", [String(b.experiments), span({ key: "c", className: "stat-cap" }, ` / ${String(b.max_experiments)}`)], "budget-experiments"),
    recorded === null ? null : stat("Original live run spent", usd(recorded.usd_spent), "budget-recorded"),
    div({ className: "bar", "aria-hidden": "true" }, div({ className: "bar-fill", style: { transform: `scaleX(${String(pct / 100)})` } })),
  );
}

function callStrip(c: ModelCall | undefined, exp: Experiment): ReactNode {
  const parts: Child[] = [];
  if (c !== undefined) {
    const tokens = (c.input_tokens ?? 0) + (c.output_tokens ?? 0);
    parts.push(
      span({ key: "who", className: "strip-who" }, "adversary"),
      span({ key: "m" }, c.model ?? "?"),
      span({ key: "p" }, c.provider ?? "?"),
      span({ key: "l" }, c.latency_ms === null || c.latency_ms === undefined ? "—" : `${(c.latency_ms / 1000).toFixed(1)} s`),
      span({ key: "t" }, `${tokens.toLocaleString("en-US")} tok`),
      span({ key: "u" }, usd(c.usd)),
    );
    if (c.replayed === true) parts.push(span({ key: "r", className: "strip-replayed" }, "replayed · recorded latency & cost · live cost $0"));
  }
  if ((exp.model_calls ?? 0) > 0) {
    parts.push(span({ key: "tgt", className: "strip-target" }, `target agent: ${plural(exp.model_calls ?? 0, "model call")}`));
  }
  if (parts.length === 0) return null;
  return div({ className: "strip", "data-testid": "model-call-strip" }, ...parts);
}

function experimentCard(exp: Experiment, index: number, all: readonly Experiment[]): ReactNode {
  const firstSame = all.findIndex((e) => e.proposed_by?.request_id !== undefined && e.proposed_by.request_id === exp.proposed_by?.request_id);
  const shared = firstSame !== -1 && firstSame < index;
  return h(
    "article",
    { key: exp.run_id ?? `x-${String(index)}`, className: `card card-${exp.verdict === "fail" ? "fail" : "plain"}`, "data-testid": "experiment-card" },
    div(
      { className: "card-head" },
      span({ className: "card-index" }, `Experiment ${String(index + 1)}`),
      badge(exp.verdict, exp.outcome),
    ),
    exp.hypothesis === undefined || exp.hypothesis === null
      ? p({ className: "muted" }, "No stated hypothesis recorded for this call.")
      : h("blockquote", { className: "hypothesis" }, exp.hypothesis),
    div(
      { className: "card-facts" },
      div(null, span({ className: "k" }, "Agent"), code(null, exp.agent)),
      div(null, span({ className: "k" }, "Fault schedule"), ...exp.faults.map((f, i) => code({ key: i }, faultText(f)))),
      exp.rules_failed.length === 0
        ? null
        : div(null, span({ className: "k" }, "Rules failed"), ...exp.rules_failed.map((r) => code({ key: r, className: "code-fail" }, r))),
      exp.outcome !== "executed" ? div(null, span({ className: "k" }, "Not executed"), exp.reason ?? exp.outcome) : null,
    ),
    shared
      ? div({ className: "strip", "data-testid": "model-call-strip" }, span({ className: "strip-who" }, "adversary"), span(null, `same model call as experiment ${String(firstSame + 1)} (one turn proposed both)`), (exp.model_calls ?? 0) > 0 ? span({ className: "strip-target" }, `target agent: ${plural(exp.model_calls ?? 0, "model call")}`) : null)
      : callStrip(exp.proposed_by, exp),
  );
}

function HuntView(props: {
  base: string;
  exp: Exploration | null;
  setExp: (e: Exploration | null) => void;
  go: (v: View) => void;
}): ReactNode {
  const { base, exp, setExp } = props;
  const [liveReason, setLiveReason] = useState<string | null>("checking provider…");
  const [mode, setMode] = useState<"replay" | "live">("replay");
  const [llmTargets, setLlmTargets] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    call<{ providers: Array<{ provider: string; status: string; reason?: string }> }>(base, "/v1/providers")
      .then((out) => {
        const dev = out.providers.find((row) => row.provider === "meta-dev");
        setLiveReason(dev?.status === "available" ? null : (dev?.reason ?? "no Meta key on this server"));
      })
      .catch((e: unknown) => setLiveReason(`provider check failed: ${errText(e)}`));
  }, [base]);

  const running = exp !== null && exp.status === "running";
  useEffect(() => {
    if (exp === null || exp.status !== "running") return;
    const id = exp.exploration_id;
    const timer = window.setInterval(() => {
      call<Exploration>(base, `/v1/explorations/${id}`)
        .then((next) => setExp(next))
        .catch((e: unknown) => setError(errText(e)));
    }, 400);
    return () => window.clearInterval(timer);
  }, [base, exp?.exploration_id, exp?.status]);

  const start = (): void => {
    setStarting(true);
    setError(null);
    call<Exploration>(base, "/v1/explorations", { mode, llm_targets: llmTargets, max_usd: llmTargets ? 3 : 1 })
      .then((started) => setExp({ ...started, status: "running" }))
      .catch((e: unknown) => setError(errText(e)))
      .finally(() => setStarting(false));
  };

  const resume = (): void => {
    if (exp === null) return;
    call<{ status: string }>(base, `/v1/explorations/${exp.exploration_id}/resume`, {})
      .then(() => setExp({ ...exp, status: "running" }))
      .catch((e: unknown) => setError(errText(e)));
  };

  const experiments = exp?.experiments ?? [];
  return section(
    { className: "view", "data-testid": "view-hunt" },
    div(
      { className: "controls" },
      div(
        { className: "seg", role: "group", "aria-label": "Mode" },
        span({ className: "seg-label" }, "Mode"),
        segButton("Replay · recorded, keyless", mode === "replay", () => setMode("replay"), false, "mode-replay"),
        segButton("Live · Meta Model API", mode === "live", () => setMode("live"), liveReason !== null, "mode-live"),
      ),
      div(
        { className: "seg", role: "group", "aria-label": "Target agent" },
        span({ className: "seg-label" }, "Target"),
        segButton("Muse Spark refund agent", llmTargets, () => setLlmTargets(true), false, "target-llm"),
        segButton("Seeded agents", !llmTargets, () => setLlmTargets(false), false, "target-seeded"),
      ),
      button({ onClick: start, label: starting ? "Starting…" : exp === null ? "Start hunt" : "Hunt again", disabled: starting || running, testid: "start-hunt", className: "btn-primary" }),
    ),
    liveReason === null ? null : p({ className: "muted", "data-testid": "live-reason" }, `Live mode unavailable: ${liveReason}. Replay re-runs a recorded Muse session through the real controller with zero model calls.`),
    error === null ? null : div({ className: "banner banner-error", role: "alert" }, strong(null, "Exploration error: "), error),
    exp === null
      ? p({ className: "empty" }, "No hunt yet. Start one: the Muse adversary proposes fault schedules, the controller runs each one, and deterministic ledger checks give the verdict.")
      : div(
          null,
          div(
            { className: "hunt-head" },
            div(
              { className: "hunt-title" },
              stopChip(exp),
              span({ className: "muted" }, exp.mode === "replay" ? `replay of ${exp.recording ?? "a recording"}` : `live · ${exp.provider}`),
              exp.stop_status === "paused_on_error" && exp.status === "stopped"
                ? button({ onClick: resume, label: "Resume", testid: "resume" })
                : null,
            ),
            budgetMeter(exp),
          ),
          running ? spinner(`Muse adversary is hunting… ${plural(experiments.length, "experiment")} so far`) : null,
          exp.error ? div({ className: "banner banner-error", role: "alert" }, strong(null, "Stopped with error: "), exp.error) : null,
          experiments.length === 0 && !running ? p({ className: "empty" }, "The adversary ran no experiments.") : null,
          div({ className: "feed", "data-testid": "experiment-feed" }, ...experiments.map((e, i) => experimentCard(e, i, experiments))),
          exp.final_text ? p({ className: "final-text" }, span({ className: "k" }, "Adversary's closing note"), exp.final_text) : null,
          exp.failure ? nextButton("See the failure", () => props.go("failure")) : null,
          modelLog(exp.model_calls ?? []),
        ),
  );
}

function segButton(label: string, on: boolean, onClick: () => void, disabled: boolean, testid: string): ReactNode {
  return h("button", { type: "button", className: `seg-btn${on ? " is-on" : ""}`, "aria-pressed": on, disabled, onClick, "data-testid": testid }, label);
}

function nextButton(label: string, onClick: () => void): ReactNode {
  return div({ className: "next" }, button({ onClick, label: `${label} →`, className: "btn-primary", testid: "next" }));
}

function modelLog(calls: readonly ModelCall[]): ReactNode {
  if (calls.length === 0) return null;
  return h(
    "details",
    { className: "log" },
    h("summary", null, `All model calls in this hunt (${String(calls.length)})`),
    table(
      null,
      thead(null, tr(null, th(null, "Role"), th(null, "Model"), th(null, "Provider"), th(null, "Latency"), th(null, "Tokens in / out"), th(null, "USD"), th(null, "Source"))),
      tbody(
        null,
        ...calls.map((c, i) =>
          tr(
            { key: i },
            td(null, c.role ?? "adversary"),
            td(null, code(null, c.model ?? "?")),
            td(null, c.provider ?? "?"),
            td(null, c.latency_ms === null || c.latency_ms === undefined ? "—" : `${String(c.latency_ms)} ms`),
            td(null, `${String(c.input_tokens ?? 0)} / ${String(c.output_tokens ?? 0)}`),
            td(null, usd(c.usd)),
            td(null, c.replayed === true ? "replayed" : "live"),
          ),
        ),
      ),
    ),
  );
}

function needHunt(go: (v: View) => void): ReactNode {
  return div({ className: "empty" }, p(null, "Run a hunt first: this view shows what it found."), button({ onClick: () => go("hunt"), label: "Go to Hunt" }));
}

function branchColumn(title: string, sub: string, b: Branch, other: Branch | null, divergence: number | null, kind: "base" | "fault"): ReactNode {
  const known = new Set((other?.ledger ?? []).map((r) => r.transaction_id));
  return div(
    { className: `branch branch-${kind}`, "data-testid": `branch-${kind}` },
    h3(null, title),
    p({ className: "muted" }, sub),
    div({ className: "branch-total" }, stat("Ledger total", inr(b.total_refunded_minor)), stat("Refund rows", String(b.ledger.length))),
    table(
      { className: "ledger" },
      thead(null, tr(null, th(null, "Txn"), th(null, "Amount"), th(null, "Idempotency key"))),
      tbody(
        null,
        ...b.ledger.map((row) =>
          tr(
            { key: row.transaction_id, className: kind === "fault" && !known.has(row.transaction_id) ? "row-extra" : undefined },
            td(null, code(null, row.transaction_id)),
            td(null, inr(row.amount_minor)),
            td(null, code(null, row.idempotency_key)),
          ),
        ),
      ),
    ),
    h("ol", { className: "trace-list" }, ...b.trace.map((e) => traceItem(e, divergence))),
  );
}

function traceItem(e: TraceRow, divergence: number | null): ReactNode {
  const isDiv = divergence !== null && e.event_id === divergence;
  const outcome = e.outcome.error !== undefined ? `${e.outcome.error}: ${e.outcome.message ?? ""}` : "ok";
  return li(
    { key: e.event_id, className: isDiv ? "is-divergence" : undefined },
    span({ className: "ev" }, `#${String(e.event_id)}`),
    code(null, e.tool),
    span({ className: e.outcome.error !== undefined ? "out-err" : "out-ok" }, outcome),
    isDiv ? span({ className: "div-tag" }, "earliest divergence") : null,
  );
}

function claimBanner(c: Claim | null): ReactNode {
  if (c === null) return p({ className: "muted" }, "This agent does not self-report, so there is no claim to compare. The ledger alone is the evidence.");
  const actual = `${plural(c.actual.refunds_issued, "refund")}, ${inr(c.actual.total_refunded_minor)}`;
  const claimed = c.claim === null ? "no report" : `${plural(c.claim.refunds_issued, "refund")}, ${inr(Math.round(c.claim.total_refunded * 100))}`;
  const verdict = c.verdict === "false_success" ? "FALSE SUCCESS" : c.verdict === "match" ? "CLAIM MATCHES LEDGER" : c.verdict.toUpperCase();
  return div(
    { className: `claim claim-${c.verdict === "match" ? "ok" : "bad"}`, "data-testid": "claim-banner", role: "status" },
    span(null, span({ className: "k" }, "Agent reported"), strong(null, claimed)),
    span({ className: "claim-sep", "aria-hidden": "true" }, "·"),
    span(null, span({ className: "k" }, "Ledger"), strong(null, actual)),
    span({ className: "claim-arrow", "aria-hidden": "true" }, "→"),
    span({ className: "claim-verdict" }, verdict),
  );
}

function FailureView(props: { base: string; exp: Exploration | null; go: (v: View) => void }): ReactNode {
  const { base, exp } = props;
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const runId = exp?.failure?.run_id ?? null;
  useEffect(() => {
    if (exp === null || runId === null || exp.status !== "stopped") return;
    setDetail(null);
    call<RunDetail>(base, `/v1/explorations/${exp.exploration_id}/runs/${runId}`)
      .then(setDetail)
      .catch((e: unknown) => setError(errText(e)));
  }, [base, exp?.exploration_id, runId, exp?.status]);

  if (exp === null) return needHunt(props.go);
  if (exp.failure === null || exp.failure === undefined) {
    return section({ className: "view" }, p({ className: "empty" }, `This hunt stopped with “${STOP_TEXT[exp.stop_status ?? ""] ?? exp.stop_status ?? "?"}” and found no violating run.`));
  }
  const min = exp.failure.minimized ?? {};
  return section(
    { className: "view", "data-testid": "view-failure" },
    error === null ? null : div({ className: "banner banner-error", role: "alert" }, strong(null, "Could not load the run: "), error),
    detail === null && error === null ? spinner("Loading both branches from the controller…") : null,
    detail === null
      ? null
      : div(
          null,
          claimBanner(detail.claim_check),
          p(
            { className: "fork-line" },
            "Fork Reality: one fixture, one agent ",
            code(null, detail.agent),
            ", two branches. The only difference is the fault branch arms ",
            ...detail.faults.map((f, i) => code({ key: i }, faultText(f))),
            ".",
          ),
          div(
            { className: "fork" },
            detail.baseline === null
              ? p({ className: "muted" }, "No baseline recorded.")
              : branchColumn("Baseline branch", "no faults armed", detail.baseline, null, detail.first_divergence_event_id, "base"),
            branchColumn("Fault branch", detail.faults.map(faultText).join(", "), detail.faulted, detail.baseline, detail.first_divergence_event_id, "fault"),
          ),
          detail.violated_invariants.length === 0
            ? null
            : p({ className: "violations" }, span({ className: "k" }, "Invariants violated (deterministic checks)"), ...detail.violated_invariants.map((v) => code({ key: v, className: "code-fail" }, v))),
        ),
    h2(null, "Minimized witness trace"),
    p({ className: "muted" }, `Shortest action prefix that still violates the invariant · dropped ${String(min.dropped ?? 0)} · stable: ${String(min.stable ?? false)}`),
    h(
      "ol",
      { className: "witness", "data-testid": "witness" },
      ...(min.actions ?? []).map((a, i) => li({ key: i }, code(null, a.tool), span({ className: "muted" }, JSON.stringify(a.arguments)))),
    ),
    nextButton("Why did it happen?", () => props.go("diagnosis")),
  );
}

function DiagnosisView(props: { exp: Exploration | null; go: (v: View) => void }): ReactNode {
  const { exp } = props;
  if (exp === null) return needHunt(props.go);
  const d = exp.diagnosis ?? null;
  if (d === null) return section({ className: "view" }, p({ className: "empty" }, "No diagnosis: the hunt found no failure, or diagnosis was off."));
  return section(
    { className: "view", "data-testid": "view-diagnosis" },
    div(
      { className: "explain" },
      div({ className: "explain-head" }, span({ className: "explain-label", "data-testid": "explanation-label" }, "Explanation, not evidence"), span({ className: "muted" }, `written by ${d.model ?? "?"}`)),
      h3(null, "Root cause"),
      p({ className: "explain-body" }, d.root_cause ?? d.unparsed ?? "The model returned no parsable root cause."),
      h3(null, "Proposed patch"),
      h("pre", { className: "codeblock patch" }, code(null, d.proposed_patch ?? "none proposed")),
      p({ className: "muted" }, "The model's text explains the failure. It is never used as a verdict. The evidence is the retest on the next screen, judged by the same ledger checks."),
    ),
    nextButton("Retest the fix", () => props.go("fix")),
  );
}

function capsuleName(path: string | undefined): string | null {
  if (path === undefined) return null;
  return path.replace(/^demo\/recorded\//, "");
}

function FixView(props: { base: string; exp: Exploration | null; go: (v: View) => void }): ReactNode {
  const { base, exp } = props;
  const [out, setOut] = useState<ReplayOut | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (exp === null) return needHunt(props.go);
  const rerun = exp.diagnosis?.rerun;
  // the failure summary carries no body; the matching experiment card does
  const failure = (exp.experiments ?? []).find((e) => e.run_id === exp.failure?.run_id);
  if (rerun === undefined || failure === undefined) {
    return section({ className: "view" }, p({ className: "empty" }, "No retest available: there is no diagnosed failure."));
  }
  const capsule = capsuleName(rerun.capsule);
  const retest = (): void => {
    if (capsule === null) return;
    setBusy(true);
    setError(null);
    call<ReplayOut>(base, "/v1/capsules/replay", { name: capsule })
      .then(setOut)
      .catch((e: unknown) => setError(errText(e)))
      .finally(() => setBusy(false));
  };
  const before = div(
    { className: "fix-col fix-before" },
    span({ className: "k" }, "Before"),
    code(null, failure.agent),
    badge("fail"),
    p(null, `${failure.faults.map(faultText).join(", ")}`),
    p({ className: "muted" }, `rules failed: ${failure.rules_failed.join(", ")}`),
  );
  let after: ReactNode;
  if (capsule === null) {
    // seeded agents: the rerun already ran through the controller during diagnosis
    after = div(
      { className: "fix-col fix-after", "data-testid": "fix-result" },
      span({ className: "k" }, "After"),
      code(null, rerun.agent),
      badge(rerun.verdict ?? null),
      p(null, `fresh controller run ${rerun.run_id ?? "?"}, same fault schedule`),
      p({ className: "muted" }, `rules failed: ${(rerun.rules_failed ?? []).join(", ") || "none"}`),
    );
  } else if (out === null) {
    after = div(
      { className: "fix-col fix-after" },
      span({ className: "k" }, "After"),
      code(null, rerun.agent),
      busy ? spinner("Replaying the hardened agent under the same fault…") : button({ onClick: retest, label: "Retest against hardened agent", className: "btn-primary", testid: "retest" }),
    );
  } else {
    const c = out.claim_check;
    after = div(
      { className: "fix-col fix-after", "data-testid": "fix-result" },
      span({ className: "k" }, "After"),
      code(null, rerun.agent),
      badge(out.verdict ?? null),
      p(null, (out.summary?.faults ?? []).map(faultText).join(", ")),
      c === undefined ? null : p({ className: "fix-ledger" }, `${plural(c.actual.refunds_issued, "refund")} · ${inr(c.actual.total_refunded_minor)} · claim ${c.verdict === "match" ? "matches ledger" : c.verdict}`),
      p({ className: "muted" }, `reproduced: ${String(out.reproduced)} · live model calls: ${String(out.model_calls_live)}`),
    );
  }
  return section(
    { className: "view", "data-testid": "view-fix" },
    p(
      { className: "muted" },
      capsule === null
        ? "Same fault schedule, repaired agent, judged by the same deterministic checks."
        : `Retest = the recorded run of ${rerun.agent} under the same fault (${capsule}), replayed through the real controller and ledger checks. Live model calls: 0.`,
    ),
    div({ className: "fix" }, before, span({ className: "fix-arrow", "aria-hidden": "true" }, "→"), after),
    error === null ? null : div({ className: "banner banner-error", role: "alert" }, strong(null, "Retest failed: "), error),
    nextButton("Replay the whole hunt", () => props.go("replay")),
  );
}

function ReplayView(props: { base: string; exp: Exploration | null; go: (v: View) => void }): ReactNode {
  const { base } = props;
  const [names, setNames] = useState<string[]>([]);
  const [name, setName] = useState<string>(props.exp?.recording ?? "adversary-exploration-llm.json");
  const [out, setOut] = useState<ReplayOut | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    call<{ capsules: string[] }>(base, "/v1/capsules")
      .then((r) => setNames(r.capsules))
      .catch((e: unknown) => setError(errText(e)));
  }, [base]);
  const replay = (): void => {
    setBusy(true);
    setError(null);
    setOut(null);
    call<ReplayOut>(base, "/v1/capsules/replay", { name })
      .then(setOut)
      .catch((e: unknown) => setError(errText(e)))
      .finally(() => setBusy(false));
  };
  return section(
    { className: "view", "data-testid": "view-replay" },
    p({ className: "muted" }, "A capsule holds every recorded model turn. Replay feeds them back in order through the real controller: no keys, no network. Any drift from the recording fails loudly."),
    div(
      { className: "controls" },
      h("label", { htmlFor: "capsule", className: "seg-label" }, "Capsule"),
      h(
        "select",
        { id: "capsule", value: name, onChange: (e: { target: { value: string } }) => setName(e.target.value), "data-testid": "capsule-select" },
        ...(names.length === 0 ? [name] : names).map((n) => h("option", { key: n, value: n }, n)),
      ),
      button({ onClick: replay, label: busy ? "Replaying…" : "Replay capsule", disabled: busy, testid: "replay-capsule", className: "btn-primary" }),
    ),
    busy ? spinner(`Replaying ${name} with zero live model calls…`) : null,
    error === null ? null : div({ className: "banner banner-error", role: "alert" }, strong(null, "Replay failed: "), error),
    out === null
      ? null
      : div(
          { "data-testid": "replay-result" },
          div(
            { className: "replay-head" },
            stat("reproduced", String(out.reproduced), "replay-reproduced"),
            stat("live model calls", String(out.model_calls_live), "replay-live-calls"),
            out.model_calls_replayed === undefined ? null : stat("replayed model calls", String(out.model_calls_replayed)),
          ),
          table(
            null,
            thead(null, tr(null, th(null, "Check against the recording"), th(null, "Result"))),
            tbody(null, ...Object.entries(out.matches).map(([k, v]) => tr({ key: k }, td(null, code(null, k)), td({ className: v === true ? "ok" : "bad" }, v === true ? "matches" : JSON.stringify(v))))),
          ),
        ),
    nextButton("See the evidence", () => props.go("evidence")),
  );
}

function EvidenceView(props: { base: string }): ReactNode {
  const [data, setData] = useState<EvidenceOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    call<EvidenceOut>(props.base, "/v1/evidence")
      .then(setData)
      .catch((e: unknown) => setError(errText(e)));
  }, [props.base]);
  if (error !== null) return div({ className: "banner banner-error", role: "alert" }, strong(null, "Could not load evidence: "), error);
  if (data === null) return spinner("Loading calibration and benchmark from the service…");
  const cal = data.calibration;
  const bench = data.benchmark;
  const STRAT: Record<string, string> = { random: "random (seeded shuffle)", fixed: "fixed (vulnerable first)", fixed_reversed: "fixed (repaired first)", muse: "Muse-guided (muse-spark-1.3)" };
  return section(
    { className: "view evidence", "data-testid": "view-evidence" },
    h2(null, "Calibration: the LLM agent under timeout-after-commit"),
    cal === null
      ? p({ className: "empty" }, "No calibration recorded.")
      : div(
          null,
          p({ className: "muted" }, `${cal.model} via ${cal.provider} · fault ${cal.fault.hook} · ${String(cal.model_calls)} model calls · ${usd(cal.usd_spent)} · ${cal.generated_at.slice(0, 10)}`),
          table(
            { "data-testid": "calibration-table" },
            thead(null, tr(null, th(null, "Variant"), th(null, "Prompt"), th(null, "Read path"), th(null, "n (runs)"), th(null, "Duplicate refunds"), th(null, "False success"), th(null, "No report"), th(null, "Errored"))),
            tbody(
              null,
              ...cal.table.map((row) => {
                const n = Number(row["runs"]);
                return tr(
                  { key: String(row["variant"]) },
                  td(null, code(null, String(row["variant"]))),
                  td(null, String(row["prompt"])),
                  td(null, String(row["read_path"])),
                  td(null, String(n)),
                  td({ className: Number(row["duplicates"]) > 0 ? "bad" : undefined }, `${String(row["duplicates"])}/${String(n)}`),
                  td({ className: Number(row["false_success"]) > 0 ? "bad" : undefined }, `${String(row["false_success"])}/${String(n)}`),
                  td(null, `${String(row["no_report"])}/${String(n)}`),
                  td(null, String(row["errored_runs"])),
                );
              }),
            ),
          ),
          h3(null, "What the runs show"),
          markdown(data.calibration_notes),
        ),
    h2(null, "Benchmark: random vs fixed vs Muse-guided"),
    bench === null
      ? p({ className: "empty" }, "No benchmark recorded.")
      : div(
          null,
          p({ className: "muted" }, `${String(bench.budget)} experiments per trial · ${String(bench.space_cells)} cells · ${String(bench.failing_cells.length)} failing · ${bench.generated_at.slice(0, 10)}`),
          table(
            { "data-testid": "benchmark-table" },
            thead(null, tr(null, th(null, "Strategy"), th(null, "n (trials)"), th(null, "Found a failure"), th(null, "Mean experiments to first failure"), th(null, "Max"), th(null, "USD total"))),
            tbody(
              null,
              ...Object.entries(bench.strategies).map(([k, s]) =>
                s === null
                  ? null
                  : tr(
                      { key: k },
                      td(null, STRAT[k] ?? k),
                      td(null, String(s.trials)),
                      td(null, `${String(s.discovered)}/${String(s.trials)}`),
                      td(null, s.mean_experiments_to_first_failure === null ? "—" : s.mean_experiments_to_first_failure.toFixed(2)),
                      td(null, s.max_experiments_to_first_failure === null ? "—" : String(s.max_experiments_to_first_failure)),
                      td(null, usd(s.usd_total, 6)),
                    ),
              ),
            ),
          ),
          h3(null, "Caveats (they matter more than the table)"),
          markdown(data.benchmark_caveats),
        ),
  );
}

/** The step 6 story: one shared exploration across six views. */
export function HuntFlow(props: { base: string }): ReactNode {
  const [view, setView] = useState<View>("hunt");
  const [exp, setExp] = useState<Exploration | null>(null);
  const go = (v: View): void => {
    setView(v);
    window.scrollTo(0, 0);
  };
  const found = exp?.failure !== null && exp?.failure !== undefined;
  const body =
    view === "hunt"
      ? createElement(HuntView, { base: props.base, exp, setExp, go })
      : view === "failure"
        ? createElement(FailureView, { base: props.base, exp, go })
        : view === "diagnosis"
          ? createElement(DiagnosisView, { exp, go })
          : view === "fix"
            ? createElement(FixView, { base: props.base, exp, go })
            : view === "replay"
              ? createElement(ReplayView, { base: props.base, exp, go })
              : createElement(EvidenceView, { base: props.base });
  return div(
    { className: "hunt" },
    h(
      "nav",
      { className: "tabs", "aria-label": "Story views" },
      ...VIEWS.map(([v, label], i) =>
        h(
          "button",
          { key: v, type: "button", className: `tab${view === v ? " is-on" : ""}`, "aria-current": view === v ? "page" : undefined, onClick: () => go(v), "data-testid": `tab-${v}` },
          span({ className: "tab-n" }, String(i + 1)),
          label,
          v === "failure" && found ? span({ className: "tab-dot", "aria-label": "failure found" }) : null,
        ),
      ),
    ),
    body,
  );
}
