import type { RepairView } from "../api/api.generated.ts";

export type Tone = "ok" | "warn" | "fail" | "active" | "neutral";

export interface RecordedField {
  label: string;
  /** The recorded value, verbatim, or a label saying that nothing was recorded. */
  value: string;
  recorded: boolean;
}

export interface RepairPresentation {
  tone: Tone;
  headline: string;
  detail: string;
  fields: RecordedField[];
}

function field(label: string, value: unknown, whenNull: string): RecordedField {
  return value === null || value === undefined
    ? { label, value: whenNull, recorded: false }
    : { label, value: String(value), recorded: true };
}

function lostFaultsField(repair: RepairView): RecordedField {
  const lost = repair.lost_faults;
  if (lost === null) return { label: "lost_faults", value: "not recorded yet", recorded: false };
  return { label: "lost_faults", value: lost.length === 0 ? "none" : lost.join(", "), recorded: true };
}

function heldByField(repair: RepairView, isRefund: boolean): RecordedField {
  if (repair.held_by !== null) return { label: "held_by", value: repair.held_by, recorded: true };
  const value = isRefund ? "not recorded for this world" : "not set (only a verified repair sets it)";
  return { label: "held_by", value, recorded: false };
}

/**
 * Success needs the verifier's verdict and the controller's outcome to agree. Anything
 * else, including an inconsistent record, is shown as not successful. Nothing here
 * recomputes a verdict: the inputs are read and compared, never derived from evidence.
 */
export function presentRepair(
  repair: RepairView,
  outcome: string | null,
  worldVersion: string,
): RepairPresentation {
  const fields = [
    field("run_status", repair.run_status, "not recorded yet"),
    field("safety_held", repair.safety_held, "not recorded yet"),
    field("task_complete", repair.task_complete, "not recorded yet"),
    field("verified", repair.verified, "not recorded yet"),
    lostFaultsField(repair),
    heldByField(repair, worldVersion === "refund-world/1"),
  ];
  const lost = repair.lost_faults ?? [];
  const agrees =
    repair.verified === true &&
    outcome === "repair_verified" &&
    repair.run_status === "completed" &&
    repair.safety_held === true &&
    lost.length === 0;

  if (agrees && repair.lost_faults === null) {
    return {
      tone: "warn",
      headline: "Repair verified, fault coverage not recorded",
      detail: "This older record doesn't say whether the repair met every fault the fork met.",
      fields,
    };
  }
  if (agrees && repair.task_complete === true) {
    return {
      tone: "ok",
      headline: "Repair verified",
      detail: "The run finished, the safety checks passed and the verifier marked the repair verified.",
      fields,
    };
  }
  if (agrees) {
    return {
      tone: "warn",
      headline: "Repair verified, task not complete",
      detail: "The repair held, but the recorded task did not finish. Each unfinished part is listed below.",
      fields,
    };
  }
  if (repair.verified === true) {
    return {
      tone: "fail",
      headline: "Inconsistent repair record",
      detail: `The record says verified, but the campaign outcome is ${outcome ?? "not set"} and it lists ${lost.length} lost faults. Treat this as not verified.`,
      fields,
    };
  }
  if (outcome === "repair_did_not_hold") {
    return {
      tone: "fail",
      headline: "Repair did not hold",
      detail: "The repaired run finished and a safety check failed.",
      fields,
    };
  }
  if (outcome === "repair_incomplete") {
    return {
      tone: "warn",
      headline: "Repair incomplete",
      detail:
        lost.length > 0
          ? `The repaired run never met ${lost.join(", ")}, which the fork met, so the repair was not tested against it. Passing checks on this run prove nothing.`
          : "The repaired run did not get far enough to test the repair. Passing checks on this run prove nothing.",
      fields,
    };
  }
  if (repair.run_status === null) {
    return {
      tone: "active",
      headline: "Repair authorized, no result yet",
      detail: "Nothing has been recorded for the repaired run.",
      fields,
    };
  }
  return {
    tone: "warn",
    headline: "Repair not verified",
    detail: `The campaign outcome is ${outcome ?? "not set"}.`,
    fields,
  };
}
