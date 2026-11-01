import type { WorldRealityDiff, WorldRunResult } from "../api/api.generated.ts";
import { metricRows, refundLedger, type AlignedRow } from "./evidence.ts";

function recordOf(event: AlignedRow["baseline"]): string {
  if (event === null) return "absent";
  return JSON.stringify({ request: event.request, outcome: event.outcome, state: event.state });
}

/**
 * Whether the two recorded event records differ in request, outcome or state. A
 * missing side differs. This compares records; it never says which side is right.
 */
export function alignedRowDiffers(row: AlignedRow): boolean {
  return recordOf(row.baseline) !== recordOf(row.fork);
}

export interface AlignedSummary {
  total: number;
  differing: number;
  differingIds: number[];
}

export function summarizeAligned(rows: readonly AlignedRow[]): AlignedSummary {
  const differingIds = rows.filter(alignedRowDiffers).map((row) => row.eventId);
  return { total: rows.length, differing: differingIds.length, differingIds };
}

export interface ChangedMetric {
  name: string;
  baseline: number | null;
  fork: number | null;
  /** Fork minus baseline. Null when either side was not recorded. Computed, not recorded. */
  delta: number | null;
}

/** Recorded metrics whose two sides are not equal. Equal metrics are left out. */
export function changedMetrics(diff: WorldRealityDiff): ChangedMetric[] {
  return metricRows(diff)
    .filter((row) => row.baseline !== row.fork)
    .map((row) => ({
      ...row,
      delta: row.baseline === null || row.fork === null ? null : row.fork - row.baseline,
    }));
}

export interface LedgerComparison {
  baselineCount: number;
  forkCount: number;
  baselineOnly: string[];
  forkOnly: string[];
}

/**
 * Compares recorded refund ledgers by transaction id. Null outside refund-world
 * or while a side is still loading, when a comparison would mislead.
 */
export function compareLedgers(baseline: WorldRunResult | null, fork: WorldRunResult | null): LedgerComparison | null {
  const left = refundLedger(baseline);
  const right = refundLedger(fork);
  if (left === null || right === null) return null;
  const leftIds = new Set(left.rows.map((row) => row.transaction_id));
  const rightIds = new Set(right.rows.map((row) => row.transaction_id));
  return {
    baselineCount: left.rows.length,
    forkCount: right.rows.length,
    baselineOnly: [...leftIds].filter((id) => !rightIds.has(id)),
    forkOnly: [...rightIds].filter((id) => !leftIds.has(id)),
  };
}
