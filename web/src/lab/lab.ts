/* /lab — the staged demo. One scene per screen, every number from the API
 * (replay of demo/recorded/* by default; live when the server holds a key).
 * The stage animates recorded runs step by step; it never invents data. */

import { createElement, useEffect, useRef, useState, type ReactNode } from "react";
import { h, type Child } from "../ui";

// ---- wire shapes (docs/api.md) ------------------------------------------------

interface Fault { hook: string; trigger?: { tool?: string; occurrence?: number } }
interface ModelCall { model?: string | null; provider?: string | null; latency_ms?: number | null; input_tokens?: number | null; output_tokens?: number | null; usd?: number | null; replayed?: boolean; request_id?: string | null }
interface Experiment { run_id: string | null; agent: string; faults: Fault[]; outcome: string; verdict: string | null; rules_failed: string[]; hypothesis?: string | null; proposed_by?: ModelCall }
interface Budget { usd_spent: number; usd_cap: number; experiments: number; max_experiments: number }
interface Exploration {
  exploration_id: string; mode: string; provider: string; status: string; stop_status?: string | null; recording?: string | null;
  experiments?: Experiment[]; budget?: Budget; recorded_budget?: Budget | null;
  failure?: { run_id: string; minimized?: { required_prefix?: string[]; actions?: Array<{ tool: string; arguments: Record<string, unknown> }> } } | null;
  diagnosis?: { model?: string | null; root_cause?: string | null; proposed_patch?: string | null; rerun?: { agent: string; capsule?: string; verdict?: string } } | null;
  error?: string | null;
}
interface TraceRow { event_id: number; tool: string; arguments: Record<string, unknown>; outcome: { result?: Record<string, unknown>; error?: string; message?: string }; ledger_total_after: number | null; ledger_rows_after: number | null }
interface Claim { verdict: string; claim: { refunds_issued: number; total_refunded: number } | null; actual: { refunds_issued: number; total_refunded_minor: number } }
interface Branch { ledger: Array<{ amount_minor: number }>; total_refunded_minor: number; trace: TraceRow[] }
interface RunDetail { agent: string; faults: Fault[]; baseline: Branch | null; faulted: Branch; first_divergence_event_id: number | null; violated_invariants: string[]; claim_check: Claim | null }
interface ReplayOut { name: string; reproduced: boolean; verdict?: string; claim_check?: Claim; model_calls_live: number; model_calls_replayed?: number; trace?: TraceRow[]; agent?: string; error?: string }
interface Stats { trials: number; discovered: number; mean_experiments_to_first_failure: number | null }
interface EvidenceOut {
  calibration: { model: string; table: Array<Record<string, string | number>> } | null;
  benchmark: { budget: number; space_cells: number; strategies: Record<string, Stats> } | null;
  benchmark_caveats: string | null;
}

async function call<T>(path: string, body?: unknown): Promise<T> {
  const init: RequestInit = body === undefined ? {} : { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, init);
  const data = (await res.json().catch(() => null)) as (T & { detail?: string; error?: string }) | null;
  if (!res.ok || data === null) throw new Error(data?.detail ?? data?.error ?? `HTTP ${String(res.status)} on ${path}`);
  return data;
}

const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e));
const inr = (minor: number): string => `₹${(minor / 100).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
const plural = (n: number, w: string): string => `${String(n)} ${w}${n === 1 ? "" : "s"}`;
const faultText = (f: Fault): string => `${f.hook} on ${f.trigger?.tool ?? "*"} #${String(f.trigger?.occurrence ?? "?")}`;
const REDUCED = typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const REPO = "https://github.com/GhostLab-Universe/ghostlab-universe";

const d = (cls: string, ...children: Child[]): ReactNode => h("div", { className: cls }, ...children);
const s = (cls: string, ...children: Child[]): ReactNode => h("span", { className: cls }, ...children);

/** Milliseconds since `key` last changed (scene entry); jumps to the end under reduced motion. */
function useClock(key: string, running: boolean): number {
  const [t, setT] = useState(0);
  const start = useRef(0);
  useEffect(() => {
    start.current = performance.now();
    setT(REDUCED ? 1e9 : 0);
    if (!running || REDUCED) return;
    const id = window.setInterval(() => setT(performance.now() - start.current), 50);
    return () => window.clearInterval(id);
  }, [key, running]);
  return t;
}

/** Number that eases to its target in ~450 ms. */
function useTween(target: number): number {
  const [v, setV] = useState(target);
  const from = useRef(target);
  useEffect(() => {
    if (REDUCED) { setV(target); return; }
    const a = from.current, t0 = performance.now();
    let raf = 0;
    const tick = (now: number): void => {
      const k = Math.min(1, (now - t0) / 450);
      const val = a + (target - a) * (1 - Math.pow(1 - k, 3));
      setV(val);
      from.current = val;
      if (k < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target]);
  return v;
}

// ---- sequence diagram ---------------------------------------------------------

interface Step { kind: "call" | "report"; tool: string; error: string | null; total: number; rows: number; added: number; over: boolean; claim?: string }

function stepsFrom(trace: readonly TraceRow[], claim: Claim | null): Step[] {
  const paid = Number(trace[0]?.outcome.result?.["paid_minor"] ?? NaN);
  let total = 0, rows = 0;
  const out: Step[] = trace.map((e) => {
    const nextTotal = e.ledger_total_after ?? total;
    const nextRows = e.ledger_rows_after ?? rows;
    const step: Step = { kind: "call", tool: e.tool, error: e.outcome.error ?? null, total: nextTotal, rows: nextRows, added: nextRows > rows ? nextTotal - total : 0, over: Number.isFinite(paid) && nextTotal > paid };
    total = nextTotal; rows = nextRows;
    return step;
  });
  if (claim?.claim) {
    out.push({ kind: "report", tool: "report_outcome", error: null, total, rows, added: 0, over: false, claim: `“${plural(claim.claim.refunds_issued, "refund")}, ${inr(Math.round(claim.claim.total_refunded * 100))}”` });
  }
  return out;
}

const LANE = { agent: 150, boundary: 500, pay: 850 };
const ROW = 96;
const TOP = 96;

function arrow(x1: number, x2: number, y: number, cls: string, label: string | null, key: string, delay: number): ReactNode {
  const dir = x2 > x1 ? 1 : -1;
  return h(
    "g",
    { key, className: `seq-msg ${cls}`, style: { animationDelay: `${String(delay)}ms` } },
    h("line", { x1, y1: y, x2: x2 - dir * 10, y2: y }),
    h("path", { d: `M${String(x2)} ${String(y)} l${String(-dir * 12)} -6 v12 z`, className: "seq-head" }),
    label === null ? null : h("text", { x: (LANE.agent + LANE.boundary) / 2, y: y - 10, textAnchor: "middle" }, label),
  );
}

function stepGraphic(step: Step, i: number): ReactNode {
  const y = TOP + i * ROW + 20;
  const back = y + 40;
  const parts: ReactNode[] = [];
  if (step.kind === "report") {
    parts.push(arrow(LANE.agent, LANE.boundary, y + 20, "msg-muse", `report_outcome ${step.claim ?? ""}`, "r", 0));
    parts.push(h("text", { key: "n", x: LANE.boundary + 18, y: y + 26, className: "seq-note" }, "kept by GhostLab — never reaches payments"));
    return h("g", { key: `s${String(i)}` }, ...parts);
  }
  parts.push(arrow(LANE.agent, LANE.pay, y, "msg-req", step.tool, "q", 0));
  if (step.added > 0) {
    parts.push(h("g", { key: "c", className: `seq-msg commit ${step.over ? "commit-bad" : "commit-ok"}`, style: { animationDelay: "280ms" } },
      h("circle", { cx: LANE.pay, cy: y + 20, r: 7 }),
      h("text", { x: LANE.pay + 16, y: y + 25 }, `commits +${inr(step.added)}`)));
  }
  if (step.error !== null) {
    parts.push(arrow(LANE.pay, LANE.boundary + 14, back, "msg-timeout", null, "b1", 420));
    parts.push(h("g", { key: "x", className: "seq-msg cut", style: { animationDelay: "560ms" } },
      h("rect", { x: LANE.boundary - 58, y: back - 15, width: 116, height: 30, rx: 4 }),
      h("text", { x: LANE.boundary, y: back + 6, textAnchor: "middle" }, "TIMEOUT")));
    parts.push(arrow(LANE.boundary - 60, LANE.agent, back, "msg-timeout dashed", "“timeout” — no receipt", "b2", 700));
  } else {
    parts.push(arrow(LANE.pay, LANE.agent, back, "msg-ok", "ok", "b", 420));
  }
  return h("g", { key: `s${String(i)}` }, ...parts);
}

function Sequence(props: { steps: Step[]; shown: number; agentLabel: string }): ReactNode {
  const { steps, shown } = props;
  const height = TOP + Math.max(steps.length, 4) * ROW + 10;
  const lanes: Array<[number, string, string]> = [
    [LANE.agent, "Agent", props.agentLabel],
    [LANE.boundary, "GhostLab boundary", "faults injected here"],
    [LANE.pay, "Payment service", "the ledger"],
  ];
  return h(
    "svg",
    { className: "seq", viewBox: `0 0 1000 ${String(height)}`, role: "img", "aria-label": "Sequence of the agent's tool calls through the GhostLab boundary to the payment service" },
    ...lanes.map(([x, title, sub]) =>
      h("g", { key: title, className: `lane ${title === "GhostLab boundary" ? "lane-boundary" : ""}` },
        h("text", { x, y: 30, textAnchor: "middle", className: "lane-title" }, title),
        h("text", { x, y: 54, textAnchor: "middle", className: "lane-sub" }, sub),
        h("line", { x1: x, y1: 70, x2: x, y2: height - 6 }))),
    ...steps.slice(0, shown).map(stepGraphic),
  );
}

function Ledger(props: { steps: Step[]; shown: number }): ReactNode {
  const visible = props.steps.slice(0, props.shown).filter((st) => st.kind === "call");
  const last = visible[visible.length - 1];
  const total = useTween(last?.total ?? 0);
  const rows = visible.filter((st) => st.added > 0);
  return d(
    "ledger",
    s("ledger-label", "Payment ledger"),
    h("div", { className: `ledger-total ${last?.over === true ? "is-bad" : ""}`, "data-testid": "ledger-total" }, inr(Math.round(total))),
    h("ol", { className: "ledger-rows" }, ...rows.map((r, i) => h("li", { key: i, className: r.over ? "row-bad" : "row-ok" }, s("mono", `refund-${String(i + 1)}`), s("", inr(r.added))))),
  );
}

// ---- scenes -------------------------------------------------------------------

interface Data {
  exp: Exploration | null;
  detail: RunDetail | null;
  fix: ReplayOut | null;
  cap: ReplayOut | null;
  evidence: EvidenceOut | null;
  error: string | null;
}

function wait(label: string): ReactNode {
  return d("wait", s("lspin", ""), s("", label));
}

function Hero(props: { onRun: () => void; busy: boolean }): ReactNode {
  return d(
    "scene scene-hero",
    h("h1", { className: "hero-title" }, "Your agent says it refunded once. ", h("span", { className: "hl-bad" }, "The ledger says twice.")),
    h("p", { className: "hero-sub" }, "GhostLab crash-tests AI agents that take real actions: it injects the faults production will throw at them and proves what actually happened."),
    h("button", { type: "button", className: "cta", onClick: props.onRun, disabled: props.busy, "data-testid": "run-crash-test" }, props.busy ? "Starting the hunt…" : "Run the crash test"),
    h("p", { className: "hero-credits" }, "Muse Spark 1.3 · Muse Wonder 1.0 · Meta Model API"),
  );
}

function Hunt(props: { data: Data; sceneKey: string }): ReactNode {
  const { exp, detail } = props.data;
  const ready = exp !== null && exp.status === "stopped" && detail !== null;
  const t = useClock(`${props.sceneKey}-${String(ready)}`, ready);
  const experiments = exp?.experiments ?? [];
  const hyps = [...new Set(experiments.map((e) => e.hypothesis).filter((x): x is string => typeof x === "string"))];
  const text = hyps.join("\n");
  const typed = ready ? text.slice(0, Math.floor(t / 28)) : "";
  const typeEnd = text.length * 28 + 300;
  const lchipAt = (i: number): number => typeEnd + i * 650;
  const seqStart = lchipAt(experiments.length) + 300;
  const steps = detail === null ? [] : stepsFrom(detail.faulted.trace, detail.claim_check);
  const shown = ready ? Math.max(0, Math.min(steps.length, Math.floor((t - seqStart) / 1500) + 1)) : 0;
  const b = exp?.budget;
  const recorded = exp?.recorded_budget ?? null;
  const usdNow = exp?.mode === "replay" ? (recorded?.usd_spent ?? 0) : (b?.usd_spent ?? 0);
  const usdCap = b?.usd_cap ?? 1;
  return d(
    "scene scene-hunt",
    d(
      "muse",
      d("muse-head", s("muse-dot", ""), s("muse-name", "Muse adversary"), s("muse-model", experiments[0]?.proposed_by?.model ?? "muse-spark-1.3")),
      exp === null ? wait("Starting the hunt…") : !ready ? wait(`Muse adversary is hunting… ${plural(experiments.length, "experiment")}`) : null,
      h("p", { className: "muse-hyp", "data-testid": "hypothesis" }, typed, ready && typed.length < text.length ? s("caret", "") : null),
      d(
        "lchips",
        ...experiments.map((e, i) => {
          if (!ready || t < lchipAt(i)) return null;
          const resolved = t > lchipAt(i) + 420;
          const v = e.outcome !== "executed" ? "untested" : (e.verdict ?? "untested");
          return h("div", { key: e.run_id ?? i, className: `lchip ${resolved ? `lchip-${v}` : "lchip-pending"}`, "data-testid": "exp-chip" },
            s("mono", e.agent), s("lchip-fault", e.faults.map(faultText).join(", ")),
            s("lchip-verdict", resolved ? v.toUpperCase() : "running…"));
        }),
      ),
      b === undefined ? null : d(
        "budget",
        d("budget-row",
          s("budget-k", exp?.mode === "replay" ? "USD (recorded live run)" : "USD spent"),
          s("budget-v", `$${usdNow.toFixed(4)} / $${usdCap.toFixed(2)}`),
          s("budget-k", "experiments"),
          s("budget-v", `${String(b.experiments)} / ${String(b.max_experiments)}`)),
        d("budget-bar", h("div", { className: "budget-fill", style: { transform: `scaleX(${String(Math.min(1, usdNow / usdCap))})` } })),
      ),
    ),
    d(
      "stage",
      detail === null ? wait("Waiting for the failing run…") : createElement(Sequence, { steps, shown, agentLabel: "Muse Spark 1.3" }),
      detail === null ? null : createElement(Ledger, { steps, shown }),
    ),
  );
}

function Verdict(props: { data: Data }): ReactNode {
  const det = props.data.detail;
  if (det === null) return d("scene", wait("Waiting for the hunt to finish…"));
  const c = det.claim_check;
  const claimed = c?.claim ? `${plural(c.claim.refunds_issued, "refund")} · ${inr(Math.round(c.claim.total_refunded * 100))}` : "no report";
  const actual = `${plural(det.faulted.ledger.length, "refund")} · ${inr(det.faulted.total_refunded_minor)}`;
  return d(
    "scene scene-verdict",
    h("h2", { className: "verdict-word", "data-testid": "verdict-word" }, c?.verdict === "false_success" ? "FALSE SUCCESS" : (c?.verdict ?? "VIOLATION").toUpperCase().replace("_", " ")),
    d("face",
      d("face-side", s("face-k", "Agent reported"), s("face-v", claimed)),
      s("face-vs", "vs"),
      d("face-side face-bad", s("face-k", "Ledger"), s("face-v", actual))),
    d("inv", ...det.violated_invariants.map((v) => s("inv-chip", v))),
    h("p", { className: "verdict-note" }, "Decided by deterministic checks over the ledger — never by a model."),
  );
}

function Fork(props: { data: Data }): ReactNode {
  const { detail: det, exp } = props.data;
  if (det === null || det.baseline === null) return d("scene", wait("Waiting for both branches…"));
  const base = det.baseline, fault = det.faulted;
  const div = det.first_divergence_event_id;
  const paid = Number(base.trace[0]?.outcome.result?.["paid_minor"] ?? 0);
  const after = (b: Branch): TraceRow[] => b.trace.filter((e) => div === null || e.event_id >= div);
  const dots = (b: Branch, y: number, cls: string): ReactNode[] =>
    after(b).map((e, i) => h("g", { key: `${cls}${String(i)}`, className: `lfork-ev ${cls}`, style: { animationDelay: `${String(700 + i * 250)}ms` } },
      h("circle", { cx: 700 + i * 260, cy: y, r: 9, className: e.outcome.error ? "ev-timeout" : "" }),
      h("text", { x: 700 + i * 260, y: y - 22, textAnchor: "middle" }, `#${String(e.event_id)} ${e.tool}`)));
  const lwit = exp?.failure?.minimized?.actions ?? [];
  return d(
    "scene scene-fork",
    h("h2", { className: "scene-title" }, "Fork Reality: same snapshot, two timelines"),
    h(
      "svg",
      { className: "lfork", viewBox: "0 0 1500 420", role: "img", "aria-label": "Baseline and fault branches from one snapshot" },
      h("path", { d: "M150 210 H430", className: "lfork-trunk" }),
      h("path", { d: "M430 210 C 500 210, 520 110, 600 110 H1170", className: "lfork-good draw", style: { animationDelay: "200ms" } }),
      h("path", { d: "M430 210 C 500 210, 520 310, 600 310 H1170", className: "lfork-bad draw", style: { animationDelay: "350ms" } }),
      h("circle", { cx: 150, cy: 210, r: 16, className: "lfork-node" }),
      h("text", { x: 150, y: 262, textAnchor: "middle", className: "lfork-label" }, "snapshot"),
      h("text", { x: 150, y: 290, textAnchor: "middle", className: "lfork-sub" }, `paid ${inr(paid)} · ledger empty`),
      h("g", { className: "lfork-div" },
        h("rect", { x: 418, y: 198, width: 24, height: 24, transform: "rotate(45 430 210)" }),
        h("text", { x: 400, y: 160, textAnchor: "end" }, `${det.faults.map(faultText).join(", ")}`),
        h("text", { x: 400, y: 132, textAnchor: "end", className: "lfork-sub" }, div === null ? "divergence" : `diverges at event #${String(div)}`)),
      ...dots(base, 110, "ev-good"),
      ...dots(fault, 310, "ev-bad"),
      h("g", { className: "lfork-end good" },
        h("text", { x: 1190, y: 104, className: "lfork-end-v" }, `${plural(base.ledger.length, "refund")} · ${inr(base.total_refunded_minor)}`),
        h("text", { x: 1190, y: 132, className: "lfork-sub" }, "baseline · no faults")),
      h("g", { className: "lfork-end bad" },
        h("text", { x: 1190, y: 304, className: "lfork-end-v" }, `${plural(fault.ledger.length, "refund")} · ${inr(fault.total_refunded_minor)}`),
        h("text", { x: 1190, y: 332, className: "lfork-sub" }, "fault branch")),
    ),
    d("lwit",
      s("lwit-k", `Minimized witness · ${plural(lwit.length, "step")}`),
      ...lwit.map((a, i) => h("span", { key: i, className: "lwit-step" }, s("lwit-n", String(i + 1)), s("mono", a.tool), typeof a.arguments["idempotency_key"] === "string" ? s("lwit-arg", `key ${String(a.arguments["idempotency_key"])}`) : null))),
  );
}

function Diagnosis(props: { data: Data }): ReactNode {
  const dg = props.data.exp?.diagnosis ?? null;
  if (dg === null) return d("scene", wait("Waiting for the diagnosis…"));
  const patch = dg.proposed_patch ?? "";
  const [prose, code] = patch.includes("Pseudo-code:") ? patch.split("Pseudo-code:") as [string, string] : [patch, ""];
  const codeLines = code.split(/;\s*/).map((l) => l.trim()).filter((l) => l !== "");
  return d(
    "scene scene-diag",
    d("diag-card",
      d("diag-head", s("diag-who", dg.model ?? "muse-wonder"), s("diag-tag", "Explanation, not evidence")),
      h("h2", { className: "diag-title" }, "Why the agent paid twice"),
      h("p", { className: "diag-cause" }, dg.root_cause ?? "No parsable root cause returned."),
      d("diff",
        d("diff-head", "proposed patch"),
        h("p", { className: "diff-prose" }, prose.trim()),
        ...codeLines.map((l, i) => h("div", { key: i, className: "diff-line" }, s("diff-sign", "+"), s("mono", l))))),
    h("p", { className: "verdict-note" }, "A model wrote this. The next scene is the evidence: the same fault, judged by the same ledger checks."),
  );
}

function Fix(props: { data: Data; sceneKey: string }): ReactNode {
  const { fix, exp, detail } = props.data;
  const t = useClock(props.sceneKey, fix !== null);
  if (fix === null) return d("scene", wait("Replaying the hardened agent…"));
  const steps = stepsFrom(fix.trace ?? [], fix.claim_check ?? null);
  const shown = Math.max(0, Math.min(steps.length, Math.floor((t - 300) / 1400) + 1));
  const done = shown >= steps.length;
  const failed = exp?.experiments?.find((e) => e.run_id === exp.failure?.run_id);
  return d(
    "scene scene-fix",
    d("fix-left",
      h("h2", { className: "scene-title" }, "Same fault. Hardened agent."),
      d("ba",
        d("ba-card ba-before", s("ba-k", "before"), s("mono", failed?.agent ?? detail?.agent ?? "?"), s("lbadge lbadge-fail", "FAIL"), s("ba-sub", detail === null ? "" : `${plural(detail.faulted.ledger.length, "refund")} · ${inr(detail.faulted.total_refunded_minor)}`)),
        d(`ba-card ba-after ${done ? "is-in" : ""}`, s("ba-k", "after"), s("mono", fix.agent ?? "?"), s("lbadge lbadge-pass", (fix.verdict ?? "?").toUpperCase()),
          s("ba-sub", fix.claim_check ? `${plural(fix.claim_check.actual.refunds_issued, "refund")} · ${inr(fix.claim_check.actual.total_refunded_minor)}` : ""))),
      h("p", { className: `fix-claim ${done ? "is-in" : ""}`, "data-testid": "fix-claim" }, fix.claim_check?.verdict === "match" ? "claim matches ledger" : (fix.claim_check?.verdict ?? "")),
      h("p", { className: "verdict-note" }, `Recorded run, replayed through the real controller · live model calls: ${String(fix.model_calls_live)}`)),
    d("stage", createElement(Sequence, { steps, shown, agentLabel: fix.agent ?? "agent" }), createElement(Ledger, { steps, shown })),
  );
}

const CAPSULE = "naive-spark-duplicate.json";

function Capsule(props: { data: Data }): ReactNode {
  const cap = props.data.cap;
  return d(
    "scene scene-capsule",
    h("h2", { className: "scene-title big" }, "This failure is now a regression test."),
    d("capsule",
      d("capsule-head", s("capsule-ico", ""), s("mono", `demo/recorded/${CAPSULE}`)),
      h("pre", { className: "capsule-cmd" }, h("code", null, `$ python -m ghostlab.llm.capsules demo/recorded/${CAPSULE}`)),
      cap === null
        ? wait("Replaying the capsule now…")
        : cap.error !== undefined
          ? d("capsule-row bad", `replay failed: ${cap.error}`)
          : h("div", { className: "capsule-row", "data-testid": "capsule-row" },
              s(cap.reproduced ? "ok" : "bad", `reproduced ${cap.reproduced ? "✓" : "✗"}`),
              s("", `verdict ${cap.verdict ?? "?"}`),
              s("", `claim ${cap.claim_check?.verdict ?? "?"}`),
              s("", `${String(cap.model_calls_live)} model calls`),
              s("", "0 keys"))),
    h("p", { className: "verdict-note" }, "Every recorded model turn is in the capsule. Replay feeds them back through the controller with no network; any drift fails loudly."),
  );
}

function strip(md: string): string {
  return md.replace(/\*\*/g, "").replace(/`/g, "");
}

function Evidence(props: { data: Data }): ReactNode {
  const ev = props.data.evidence;
  if (ev === null) return d("scene", wait("Loading calibration and benchmark…"));
  const table = ev.calibration?.table ?? [];
  const worst = [...table].sort((a, b) => Number(b["duplicates"]) - Number(a["duplicates"]))[0];
  const hardened = table.filter((r) => String(r["prompt"]) === "hardened");
  const hardDup = hardened.reduce((n, r) => n + Number(r["duplicates"]), 0);
  const hardRuns = hardened.reduce((n, r) => n + Number(r["runs"]), 0);
  const strategies = Object.entries(ev.benchmark?.strategies ?? {});
  const caveat = (ev.benchmark_caveats ?? "").split("\n").find((l) => l.startsWith("- ")) ?? "";
  return d(
    "scene scene-evidence",
    h("h2", { className: "scene-title" }, "Evidence, with its n"),
    d("ev-grid",
      worst === undefined ? null : d("ev-stat bad", s("ev-v", `${String(worst["duplicates"])}/${String(worst["runs"])}`), s("ev-k", `${String(worst["variant"])} runs duplicated the refund`)),
      worst === undefined ? null : d("ev-stat bad", s("ev-v", `${String(worst["false_success"])}/${String(worst["runs"])}`), s("ev-k", "reported success while the ledger disagreed")),
      hardRuns === 0 ? null : d("ev-stat good", s("ev-v", `${String(hardDup)}/${String(hardRuns)}`), s("ev-k", "hardened-prompt runs duplicated"))),
    strategies.length === 0 ? null : d("bench",
      s("bench-k", `Benchmark · ${String(ev.benchmark?.budget ?? "?")} experiments per trial · ${String(ev.benchmark?.space_cells ?? "?")} cells`),
      d("bench-row", ...strategies.map(([k, st]) => s("bench-item", s("mono", k), ` found ${String(st.discovered)}/${String(st.trials)}`, st.mean_experiments_to_first_failure === null ? "" : ` · ${st.mean_experiments_to_first_failure.toFixed(1)} exp`))),
      h("p", { className: "bench-caveat" }, `Caveat: ${strip(caveat.slice(2))}`)),
    h("p", { className: "ev-external" }, "In a Sep 2026 study of 25,930 agent episodes, frontier agents duplicated side effects in 56–74% of in-flight retries and reported success in 90% of those (Li, arXiv 2609.29095)."),
    d("ev-links", h("a", { href: REPO }, "GitHub repo →"), h("a", { href: "/guide/1-environment" }, "Developer console →")),
  );
}

// ---- shell --------------------------------------------------------------------

const SCENES = ["Hero", "Hunt", "Verdict", "Fork", "Diagnosis", "Fix", "Capsule", "Evidence"] as const;

export function Lab(): ReactNode {
  const [scene, setScene] = useState(0);
  const [token, setToken] = useState(0);
  const [mode, setMode] = useState<"replay" | "live">("replay");
  const [liveReason, setLiveReason] = useState<string | null>("checking…");
  const [busy, setBusy] = useState(false);
  const [data, setData] = useState<Data>({ exp: null, detail: null, fix: null, cap: null, evidence: null, error: null });
  const patch = (p: Partial<Data>): void => setData((prev) => ({ ...prev, ...p }));

  useEffect(() => {
    call<{ providers: Array<{ provider: string; status: string; reason?: string }> }>("/v1/providers")
      .then((o) => { const dev = o.providers.find((p) => p.provider === "meta-dev"); setLiveReason(dev?.status === "available" ? null : (dev?.reason ?? "no Meta key on this server")); })
      .catch((e: unknown) => setLiveReason(errText(e)));
    call<EvidenceOut>("/v1/evidence").then((evidence) => patch({ evidence })).catch((e: unknown) => patch({ error: errText(e) }));
  }, []);

  const startHunt = (): void => {
    setBusy(true);
    patch({ exp: null, detail: null, fix: null, cap: null, error: null });
    call<Exploration>("/v1/explorations", { mode, llm_targets: true, max_usd: 3 })
      .then((exp) => { patch({ exp: { ...exp, status: "running" } }); setScene(1); })
      .catch((e: unknown) => patch({ error: errText(e) }))
      .finally(() => setBusy(false));
  };

  // poll the exploration; then load the failing run and the retest
  const expId = data.exp?.exploration_id ?? null;
  const expStatus = data.exp?.status ?? null;
  useEffect(() => {
    if (expId === null) return;
    if (expStatus === "running") {
      const id = window.setInterval(() => {
        call<Exploration>(`/v1/explorations/${expId}`).then((exp) => patch({ exp })).catch((e: unknown) => patch({ error: errText(e) }));
      }, 400);
      return () => window.clearInterval(id);
    }
    const exp = data.exp;
    if (exp?.failure) {
      call<RunDetail>(`/v1/explorations/${expId}/runs/${exp.failure.run_id}`).then((detail) => patch({ detail })).catch((e: unknown) => patch({ error: errText(e) }));
    }
    const capsule = exp?.diagnosis?.rerun?.capsule?.replace(/^demo\/recorded\//, "");
    if (capsule) {
      call<ReplayOut>("/v1/capsules/replay", { name: capsule }).then((fix) => patch({ fix })).catch((e: unknown) => patch({ error: errText(e) }));
    }
    return undefined;
  }, [expId, expStatus]);

  // the capsule scene replays for real each time it opens
  useEffect(() => {
    if (SCENES[scene] !== "Capsule") return;
    patch({ cap: null });
    call<ReplayOut>("/v1/capsules/replay", { name: CAPSULE }).then((cap) => patch({ cap })).catch((e: unknown) => patch({ cap: { name: CAPSULE, reproduced: false, model_calls_live: 0, error: errText(e) } }));
  }, [scene, token]);

  const go = (n: number): void => {
    const next = Math.max(0, Math.min(SCENES.length - 1, n));
    if (next >= 1 && data.exp === null && !busy) startHunt();
    setScene(next);
  };
  const goRef = useRef(go);
  goRef.current = go;

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (e.key === "ArrowRight") goRef.current(sceneRef.current + 1);
      else if (e.key === "ArrowLeft") goRef.current(sceneRef.current - 1);
      else if (e.key === "r" || e.key === "R") {
        setData((prev) => ({ ...prev, exp: null, detail: null, fix: null, cap: null }));
        setScene(0);
        setToken((t) => t + 1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const sceneRef = useRef(scene);
  sceneRef.current = scene;

  const key = `${String(scene)}-${String(token)}`;
  const name = SCENES[scene] ?? "Hero";
  const body =
    name === "Hero" ? createElement(Hero, { onRun: startHunt, busy })
      : name === "Hunt" ? createElement(Hunt, { data, sceneKey: key })
        : name === "Verdict" ? createElement(Verdict, { data })
          : name === "Fork" ? createElement(Fork, { data })
            : name === "Diagnosis" ? createElement(Diagnosis, { data })
              : name === "Fix" ? createElement(Fix, { data, sceneKey: key })
                : name === "Capsule" ? createElement(Capsule, { data })
                  : createElement(Evidence, { data });

  return h(
    "div",
    { className: "lab", "data-scene": name },
    d("lab-top",
      h("a", { className: "lab-brand", href: "/lab", onClick: (e: { preventDefault: () => void }) => { e.preventDefault(); go(0); } }, "GhostLab"),
      d("toggle",
        h("button", { type: "button", className: mode === "replay" ? "is-on" : "", onClick: () => setMode("replay"), "data-testid": "toggle-replay" }, "Replay"),
        h("button", { type: "button", className: mode === "live" ? "is-on" : "", onClick: () => setMode("live"), disabled: liveReason !== null, title: liveReason ?? "Runs a real hunt on the Meta Model API", "data-testid": "toggle-live" }, "Live")),
      liveReason === null ? null : s("toggle-why", `live off: ${liveReason}`)),
    h("main", { className: "lab-stage", key }, body),
    data.error === null ? null : h("div", { className: "lab-error", role: "alert" }, data.error),
    h("nav", { className: "dots", "aria-label": "Scenes" },
      ...SCENES.map((n, i) => h("button", { key: n, type: "button", className: `dot ${i === scene ? "is-on" : ""}`, "aria-label": `Scene ${String(i)}: ${n}`, "aria-current": i === scene ? "step" : undefined, onClick: () => go(i), "data-testid": `dot-${String(i)}` }))),
    h("footer", { className: "lab-foot" }, "Muse Spark 1.3 (agent + adversary) · Muse Wonder 1.0 (diagnosis) · Meta Model API · built Oct 26 – Nov 4, 2026"),
  );
}
