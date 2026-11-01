import type { Coverage, RunView } from "../api/api.generated.ts";
import type { Tone } from "./repair.ts";

export const COMPLETED = "completed";

/**
 * The coverage the service recorded. Null only on a checkpoint older than coverage. A
 * record that says complete while a run didn't finish or a fault didn't fire is treated
 * as incomplete: this can only under-claim.
 */
export function readCoverage(experiment: { coverage: Coverage | null }): Coverage | null {
  const { coverage } = experiment;
  if (coverage === null) return null;
  const finished = coverage.baseline === COMPLETED && coverage.fork === COMPLETED;
  return { ...coverage, complete: coverage.complete && finished && coverage.faults_triggered !== false };
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
    coverage: {
      baseline: left,
      fork: right,
      faults_triggered: null,
      complete: left === COMPLETED && right === COMPLETED,
    },
    recorded: false,
  };
}

export interface CoverageNote {
  tone: Tone;
  headline: string;
  detail: string;
}

/** Says why, because both runs can finish and still test nothing if a fault never fired. */
function cause(coverage: Coverage): string {
  const stopped = coverage.baseline !== COMPLETED || coverage.fork !== COMPLETED;
  const unfired = coverage.faults_triggered === false;
  const statuses = `Baseline ${coverage.baseline}, fork ${coverage.fork}.`;
  if (stopped && unfired) return `${statuses} A run stopped early and a fault never fired.`;
  if (stopped) return `${statuses} A run stopped early.`;
  if (unfired) return `${statuses} Both runs finished, but a fault never fired.`;
  return `${statuses} The experiment is recorded as incomplete.`;
}

/** Null when the experiment tested what it set out to. */
export function coverageNote(coverage: Coverage, violations: number): CoverageNote | null {
  if (coverage.complete) return null;
  if (violations > 0) {
    return {
      tone: "warn",
      headline: "Coverage incomplete, violation recorded",
      detail: `${cause(coverage)} The violation it did reach stands. The rest was not tested.`,
    };
  }
  return {
    tone: "warn",
    headline: "Coverage incomplete",
    detail: `${cause(coverage)} That proves nothing, so finding no failure here is not a pass.`,
  };
}
