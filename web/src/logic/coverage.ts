import type { Coverage, RunView } from "../api/api.generated.ts";
import type { Tone } from "./repair.ts";

export const COMPLETED = "completed";

/**
 * The coverage the service recorded. Null only on a checkpoint older than coverage. A
 * record that says complete while a run didn't finish is treated as incomplete: this
 * can only under-claim.
 */
export function readCoverage(experiment: { coverage: Coverage | null }): Coverage | null {
  const { coverage } = experiment;
  if (coverage === null) return null;
  const finished = coverage.baseline === COMPLETED && coverage.fork === COMPLETED;
  return { ...coverage, complete: coverage.complete && finished };
}

export interface CoverageSource {
  coverage: Coverage;
  recorded: boolean;
}

/** On an older checkpoint, falls back to the two recorded run statuses: M05's own rule. */
export function coverageOf(
  experiment: { coverage: Coverage | null },
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
