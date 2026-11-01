import type {
  WorldInvariantFinding,
  WorldOutcomeError,
  WorldOutcomeResult,
  WorldRealityDiff,
  WorldRunResult,
  WorldTraceEvent,
} from "../api/api.generated.ts";

export function isErrorOutcome(outcome: WorldOutcomeResult | WorldOutcomeError): outcome is WorldOutcomeError {
  return "error" in outcome;
}

export function findingsForEvent(findings: readonly WorldInvariantFinding[], eventId: number): WorldInvariantFinding[] {
  return findings.filter((finding) => finding.evidence_event_ids.includes(eventId));
}

export interface ViolatedInvariant {
  invariant: string;
  /** Findings the verifier recorded for this invariant. Empty means none was supplied. */
  findings: WorldInvariantFinding[];
}

export function violatedInvariants(
  diff: WorldRealityDiff,
  findings: readonly WorldInvariantFinding[],
): ViolatedInvariant[] {
  return (diff.violated_invariants as readonly string[]).map((invariant) => ({
    invariant,
    findings: findings.filter((finding) => finding.invariant === invariant && finding.verdict === "failed"),
  }));
}

/** Events the verifier cites for its failed findings: where the violation is evidenced. */
export function violatingEventIds(findings: readonly WorldInvariantFinding[]): number[] {
  const ids = findings.filter((finding) => finding.verdict === "failed").flatMap((finding) => finding.evidence_event_ids);
  return [...new Set(ids)].sort((a, b) => a - b);
}

export interface AlignedRow {
  eventId: number;
  baseline: WorldTraceEvent | null;
  fork: WorldTraceEvent | null;
}

export function alignTraces(baseline: WorldRunResult | null, fork: WorldRunResult | null): AlignedRow[] {
  const left = new Map((baseline?.trace ?? []).map((event) => [event.event_id, event]));
  const right = new Map((fork?.trace ?? []).map((event) => [event.event_id, event]));
  return [...new Set([...left.keys(), ...right.keys()])]
    .sort((a, b) => a - b)
    .map((eventId) => ({ eventId, baseline: left.get(eventId) ?? null, fork: right.get(eventId) ?? null }));
}

export interface LedgerRowView {
  transaction_id: string;
  amount_minor: number;
  idempotency_key: string;
  order_id: string;
}

export interface RefundLedger {
  rows: LedgerRowView[];
  totalRefundedMinor: number | null;
  currency: string | null;
  paidMinor: number | null;
}

function isLedgerRow(row: unknown): row is LedgerRowView {
  const value = row as Partial<LedgerRowView> | null;
  return typeof value?.transaction_id === "string" && typeof value.amount_minor === "number";
}

/** Reads the ledger the run recorded. Totals are the recorded ones and are never summed. */
export function refundLedger(run: WorldRunResult | null): RefundLedger | null {
  if (run === null || run.world_version !== "refund-world/1") return null;
  const { ledger, total_refunded_minor: total, order } = run.final_state;
  const recorded = order as { currency?: unknown; paid_minor?: unknown } | undefined;
  return {
    rows: Array.isArray(ledger) ? ledger.filter(isLedgerRow) : [],
    totalRefundedMinor: typeof total === "number" ? total : null,
    currency: typeof recorded?.currency === "string" ? recorded.currency : null,
    paidMinor: typeof recorded?.paid_minor === "number" ? recorded.paid_minor : null,
  };
}

export interface MetricRow {
  name: string;
  baseline: number | null;
  fork: number | null;
}

/** Recorded metrics, side by side. Differences are for the reader; none is computed here. */
export function metricRows(diff: WorldRealityDiff): MetricRow[] {
  const names = [...new Set([...Object.keys(diff.baseline_metrics), ...Object.keys(diff.faulted_metrics)])].sort();
  return names.map((name) => ({
    name,
    baseline: diff.baseline_metrics[name] ?? null,
    fork: diff.faulted_metrics[name] ?? null,
  }));
}

/** M05 and the published examples write event refs as "<run_id>:<event_id>". */
export function parseEventRef(id: string): { runId: string; eventId: number } | null {
  const match = /^(.+):(\d+)$/.exec(id);
  if (match === null || match[1] === undefined || match[2] === undefined) return null;
  return { runId: match[1], eventId: Number(match[2]) };
}
