import type { ReplayResult } from "../api/api.generated.ts";
import type { Tone } from "./repair.ts";

export interface ReplayPresentation {
  tone: Tone;
  headline: string;
  detail: string;
}

/**
 * `reproduced` says only that the replay matched the recording, so it is never success
 * on its own. `run_status` says what the recording was, and the checks what it found.
 */
export function presentReplay(result: ReplayResult): ReplayPresentation {
  const failing = Object.values(result.checks).some((passed) => !passed);
  if (!result.reproduced) {
    return {
      tone: "fail",
      headline: "Not reproduced",
      detail: "The recorded evidence did not repeat, so this capsule's claims don't hold.",
    };
  }
  if (result.run_status !== "completed") {
    return {
      tone: "warn",
      headline: "Reproduced, but the recorded run stopped early",
      detail: failing
        ? "The replay matched a run that was cut short. The failure it reached still stands; the rest was not tested."
        : "The replay matched a run that was cut short. Passing checks on it prove nothing.",
    };
  }
  if (failing) {
    return {
      tone: "warn",
      headline: "Failure reproduced",
      detail: "The recorded failure repeated. Reproducing it is not a pass: these checks still fail.",
    };
  }
  return {
    tone: "neutral",
    headline: "Reproduced",
    detail: "The recorded run finished and repeated, and every check it records passed.",
  };
}
