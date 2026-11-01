import type { ExperimentEvent } from "../api/api.generated.ts";
import type { Tone } from "./repair.ts";

export interface EventKindInfo {
  label: string;
  tone: Tone;
}

/**
 * Plain labels for campaign event kinds. Tones mark workflow moments (a run
 * starting, an approval waiting), never verdicts: a recorded finding can pass.
 */
const KINDS: Record<ExperimentEvent["kind"], EventKindInfo> = {
  campaign_created: { label: "Campaign created", tone: "neutral" },
  campaign_status_changed: { label: "Status changed", tone: "neutral" },
  task_status_changed: { label: "Task update", tone: "neutral" },
  run_started: { label: "Run started", tone: "active" },
  tool_event_recorded: { label: "Tool event", tone: "neutral" },
  run_completed: { label: "Run completed", tone: "neutral" },
  finding_recorded: { label: "Finding recorded", tone: "neutral" },
  approval_requested: { label: "Approval requested", tone: "active" },
  approval_decided: { label: "Approval decided", tone: "active" },
  budget_reserved: { label: "Budget reserved", tone: "neutral" },
  budget_exhausted: { label: "Budget exhausted", tone: "warn" },
  capsule_exported: { label: "Capsule exported", tone: "neutral" },
  replay_completed: { label: "Replay completed", tone: "neutral" },
};

export function describeEventKind(kind: ExperimentEvent["kind"]): EventKindInfo {
  return KINDS[kind];
}
