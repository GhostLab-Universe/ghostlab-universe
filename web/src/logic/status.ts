import type { CampaignView, TaskSpec } from "../api/api.generated.ts";
import { BLOCKED_ENDED_HELP } from "./failure.ts";
import type { Tone } from "./repair.ts";

type CampaignStatus = CampaignView["record"]["status"];

export interface StatusLabel {
  label: string;
  tone: Tone;
}

/** "completed" means the campaign stopped, not that anything worked, so it stays neutral. */
const CAMPAIGN: Record<CampaignStatus, StatusLabel> = {
  queued: { label: "Queued", tone: "neutral" },
  running: { label: "Running", tone: "active" },
  awaiting_approval: { label: "Awaiting approval", tone: "active" },
  completed: { label: "Stopped: completed", tone: "neutral" },
  failed: { label: "Failed", tone: "fail" },
  cancelled: { label: "Cancelled", tone: "warn" },
  budget_exhausted: { label: "Budget exhausted", tone: "warn" },
};

export function campaignStatus(status: CampaignStatus, outcome: string | null = null): StatusLabel {
  if (outcome === "settings_changed") return { label: "Blocked: settings changed", tone: "warn" };
  // Recorded as budget_exhausted, but the budget may be barely touched: nothing was shown.
  if (outcome === "coverage_incomplete") return { label: "Inconclusive", tone: "warn" };
  return CAMPAIGN[status];
}

type Inference = CampaignView["inference"];

/** The campaign's own pinned mode, not the reading server's. */
export function inferenceLabel(inference: Inference): string {
  if (inference === "fixture" || inference === "scripted") return "Proposals scripted: checked-in replies, not a model";
  if (inference === "unrecorded") return "Inference not recorded for this campaign";
  return `inference ${inference}`;
}

/** Under scripted or fixture inference, model_calls counts checked-in replies. */
export function proposalCallsLabel(inference: Inference): string {
  if (inference === "fixture" || inference === "scripted") return "Scripted proposal calls";
  if (inference === "unrecorded") return "Proposal calls (source not recorded)";
  return "Model calls";
}

export interface OutcomeNotice {
  state: "blocked" | "incomplete" | "failed";
  title: string;
  detail: string;
}

/** Outcomes that need more than the status says. None of these is a clean result. */
const OUTCOMES: Record<string, OutcomeNotice> = {
  settings_changed: { state: "blocked", title: "Blocked: settings changed", detail: BLOCKED_ENDED_HELP },
  coverage_incomplete: {
    state: "incomplete",
    title: "Coverage incomplete",
    detail:
      "A run stopped early or a fault never fired. Either way this proves nothing, so finding no failure here is not a pass.",
  },
  no_experiment_ran: {
    state: "failed",
    title: "No experiment ran",
    detail: "No proposal reached execution, so nothing was tested. This is not a clean result.",
  },
  budget_check_failed: {
    state: "failed",
    title: "Budget check failed",
    detail: "The check itself failed, which is not the same as an exhausted budget. Nothing after it ran.",
  },
};

export function outcomeNotice(outcome: string | null): OutcomeNotice | null {
  return outcome === null ? null : (OUTCOMES[outcome] ?? null);
}

const TERMINAL = new Set<CampaignStatus>(["completed", "failed", "cancelled", "budget_exhausted"]);

export function isTerminal(status: CampaignStatus): boolean {
  return TERMINAL.has(status);
}

const TASK: Record<TaskSpec["status"], StatusLabel> = {
  pending: { label: "Pending", tone: "neutral" },
  ready: { label: "Ready", tone: "neutral" },
  running: { label: "Running", tone: "active" },
  awaiting_approval: { label: "Awaiting approval", tone: "active" },
  succeeded: { label: "Succeeded", tone: "neutral" },
  failed: { label: "Failed", tone: "fail" },
  cancelled: { label: "Cancelled", tone: "warn" },
  skipped: { label: "Skipped", tone: "neutral" },
};

export function taskStatus(status: TaskSpec["status"]): StatusLabel {
  return TASK[status];
}
