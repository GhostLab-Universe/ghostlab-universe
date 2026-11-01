import type { CampaignView, TaskSpec } from "../api/api.generated.ts";
import { isTerminal } from "./status.ts";

export interface ProgressLive {
  connection: "live" | "reconnecting";
  /** Events shown so far. The campaign's event_count is the total the service reports. */
  cursor: number;
}

export interface TaskCounts {
  total: number;
  succeeded: number;
  active: number;
  waiting: number;
  failed: number;
  stopped: number;
}

export interface CampaignProgress {
  terminal: boolean;
  /** The service may still change this campaign, so the view keeps refreshing. */
  updating: boolean;
  tasks: TaskCounts;
  experiments: number;
  eventsSeen: number;
  eventsTotal: number;
  /** Plain words for what the numbers above mean right now. */
  note: string;
}

const ACTIVE = new Set<TaskSpec["status"]>(["running", "awaiting_approval"]);
const WAITING = new Set<TaskSpec["status"]>(["pending", "ready"]);
const STOPPED = new Set<TaskSpec["status"]>(["cancelled", "skipped"]);

/** Buckets every recorded task status, so the counts always add up to the total. */
export function countTasks(tasks: readonly TaskSpec[]): TaskCounts {
  const counts: TaskCounts = { total: tasks.length, succeeded: 0, active: 0, waiting: 0, failed: 0, stopped: 0 };
  for (const task of tasks) {
    if (task.status === "succeeded") counts.succeeded += 1;
    else if (task.status === "failed") counts.failed += 1;
    else if (ACTIVE.has(task.status)) counts.active += 1;
    else if (WAITING.has(task.status)) counts.waiting += 1;
    else if (STOPPED.has(task.status)) counts.stopped += 1;
  }
  return counts;
}

/**
 * Progress as recorded: task and event counts plus whether the view is still
 * refreshing. Mirrors the poller's rule, so the note never promises updates the
 * poller has stopped asking for.
 */
export function summarizeProgress(view: CampaignView, live: ProgressLive): CampaignProgress {
  const terminal = isTerminal(view.record.status);
  const updating =
    live.connection === "reconnecting" ||
    !terminal ||
    view.job.state === "active" ||
    live.cursor < view.event_count;
  const note =
    live.connection === "reconnecting"
      ? "Reconnecting: these are the last counts the service gave."
      : updating
        ? "Updating from the service while the campaign runs."
        : "No further updates are expected: the record is terminal and every reported event is shown.";
  return {
    terminal,
    updating,
    tasks: countTasks(view.tasks),
    experiments: view.experiments.length,
    eventsSeen: live.cursor,
    eventsTotal: view.event_count,
    note,
  };
}
