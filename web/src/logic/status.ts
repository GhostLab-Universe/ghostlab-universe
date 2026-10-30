import type { CampaignView, TaskSpec } from "../api/api.generated.ts";
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

export function campaignStatus(status: CampaignStatus): StatusLabel {
  return CAMPAIGN[status];
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
