import type { Failure } from "../data/source.ts";

export type FailureState =
  | "blocked"
  | "denied"
  | "validation_error"
  | "not_found"
  | "conflict"
  | "cancelled"
  | "failed"
  | "reconnecting";

/** M07's stable reason for a campaign whose recorded fixture no longer matches. */
export const SETTINGS_CHANGED = "settings_changed";

export function failureState(failure: Failure): FailureState {
  if (failure.kind === "transport") return "reconnecting";
  if (failure.kind === "invalid_response") return "failed";
  if (failure.reason === SETTINGS_CHANGED) return "blocked";
  switch (failure.error.code) {
    case "unauthenticated":
    case "forbidden":
      return "denied";
    case "validation_failed":
      return "validation_error";
    case "not_found":
      return "not_found";
    case "conflict":
    case "approval_invalid":
    case "approval_required":
    case "rate_limited":
      return "conflict";
    case "cancelled":
      return "cancelled";
    default:
      return "failed";
  }
}

const TITLES: Record<FailureState, string> = {
  blocked: "Blocked: settings changed",
  denied: "Denied",
  validation_error: "Validation error",
  not_found: "Not found",
  conflict: "Not possible right now",
  cancelled: "Cancelled",
  failed: "Failed",
  reconnecting: "Reconnecting",
};

/** The campaign has stopped: a decision on it is recorded and moves nothing. */
export const CAMPAIGN_FINISHED = "campaign_finished";

/** A refused action. M07 uses one reason for three causes; only its message says which. */
export const BLOCKED_HELP =
  "Nothing ran, and any approval is still unused. If the world's settings changed, restoring them lets the campaign resume. A damaged record can't be restored. The message above says which this is.";

/** failed / settings_changed: the campaign has ended, so a resume does nothing. */
export const BLOCKED_ENDED_HELP =
  "The fixture changed before the approval was used, so the campaign stopped. The approval is unspent, and this campaign can't continue.";

export interface FailureSummary {
  state: FailureState;
  title: string;
  message: string;
  /** What this state means for the reader, when it means more than the message says. */
  help: string | null;
  /** The service's stable code and reason. Empty when no service answered. */
  code: string | null;
  reason: string | null;
  /** May a world action have committed? Shown as recorded; unknown is not "none". */
  sideEffect: string | null;
}

export function describeFailure(failure: Failure): FailureSummary {
  const state = failureState(failure);
  const base = { state, title: TITLES[state], help: state === "blocked" ? BLOCKED_HELP : null };
  if (failure.kind === "service") {
    return {
      ...base,
      message: failure.error.message,
      code: failure.error.code,
      reason: failure.reason,
      sideEffect: failure.error.side_effect,
    };
  }
  return { ...base, message: failure.message, code: null, reason: null, sideEffect: null };
}

/** A read the session may no longer make must not leave old data on screen as current. */
export function revokesAccess(failure: Failure): boolean {
  return failureState(failure) === "denied" || failureState(failure) === "not_found";
}
