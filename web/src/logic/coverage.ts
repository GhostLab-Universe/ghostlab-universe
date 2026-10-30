import type { RunView } from "../api/api.generated.ts";
import type { Coverage } from "../api/pending.ts";
import type { Tone } from "./repair.ts";

export const COMPLETED = "completed";

function isCoverage(value: unknown): value is Coverage {
  const item = value as Partial<Coverage> | null | undefined;
  return typeof item?.baseline === "string" && typeof item.fork === "string" && typeof item.complete === "boolean";
}

/**
 * Reads the coverage the service recorded, or null if it sent none. A record that says
 * complete while a run didn't finish is treated as incomplete: it can only under-claim.
 */
export function readCoverage(experiment: object): Coverage | null {
  const value = (experiment as { coverage?: unknown }).coverage;
  if (!isCoverage(value)) return null;
  const finished = value.baseline === COMPLETED && value.fork === COMPLETED;
  return { ...value, complete: value.complete && finished };
}

export interface CoverageSource {
  coverage: Coverage;
  recorded: boolean;
}

/** Falls back to the two recorded run statuses, the same rule M05 uses to set `complete`. */
export function coverageOf(
  experiment: object,
  baseline: RunView | null,
  fork: RunView | null,
): CoverageSource | null {
  const recorded = readCoverage(experiment);
  if (recorded !== null) return { coverage: recorded, recorded: true };
  if (baseline === null || fork === null) return null;
  const [left, right] = [baseline.result.status, fork.result.status];
  return {
    coverage: { baseline: left, fork: right, complete: left === COMPLETED && right === COMPLETED },
    recorded: false,
  };
}

export interface CoverageNote {
  tone: Tone;
  headline: string;
  detail: string;
}

/** Null when both runs finished: nothing needs saying beyond the recorded statuses. */
export function coverageNote(coverage: Coverage, violations: number): CoverageNote | null {
  if (coverage.complete) return null;
  const statuses = `Baseline ${coverage.baseline}, fork ${coverage.fork}.`;
  if (violations > 0) {
    return {
      tone: "warn",
      headline: "Coverage incomplete, violation recorded",
      detail: `${statuses} The violation was seen before the run stopped, so it stands. The rest of the run was not tested.`,
    };
  }
  return {
    tone: "warn",
    headline: "Coverage incomplete",
    detail: `${statuses} A run that stopped early proves nothing, so finding no failure here is not a pass.`,
  };
}
