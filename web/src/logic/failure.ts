import type { Failure } from "../data/source.ts";

export type FailureState =
  | "denied"
  | "validation_error"
  | "not_found"
  | "conflict"
  | "cancelled"
  | "failed"
  | "reconnecting";

export function failureState(failure: Failure): FailureState {
  if (failure.kind === "transport") return "reconnecting";
  if (failure.kind === "invalid_response") return "failed";
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
  denied: "Denied",
  validation_error: "Validation error",
  not_found: "Not found",
  conflict: "Not possible right now",
  cancelled: "Cancelled",
  failed: "Failed",
  reconnecting: "Reconnecting",
};

export interface FailureSummary {
  state: FailureState;
  title: string;
  message: string;
  /** The service's stable code and reason. Empty when no service answered. */
  code: string | null;
  reason: string | null;
  /** May a world action have committed? Shown as recorded; unknown is not "none". */
  sideEffect: string | null;
}

export function describeFailure(failure: Failure): FailureSummary {
  const state = failureState(failure);
  const base = { state, title: TITLES[state] };
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
