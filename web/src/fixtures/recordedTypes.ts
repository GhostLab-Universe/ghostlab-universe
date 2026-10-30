import type {
  ReplayResult,
  RepairView,
  WorldInvariantFinding,
  WorldRunResult,
} from "../api/api.generated.ts";

export interface RecordedRepair {
  campaign_status: "completed" | "failed" | "budget_exhausted";
  outcome: string;
  run: WorldRunResult;
  findings: WorldInvariantFinding[];
  repair: RepairView;
}

export interface RecordedAction {
  action: { [key: string]: unknown };
  action_digest: string;
}

/** Output of web/tests/fixtures/record_core_outputs.py. */
export interface RecordedCoreOutputs {
  source_commit: string;
  refund_baseline_run: WorldRunResult;
  refund_fork_findings: WorldInvariantFinding[];
  refund_repair: RecordedRepair;
  refund_repair_action: RecordedAction;
  refund_replay: ReplayResult;
  journey_baseline_run: WorldRunResult;
  journey_fork_findings: WorldInvariantFinding[];
  journey_repair_held_by_adapter: RecordedRepair;
  journey_repair_held_by_boundary: RecordedRepair;
  journey_repair_truncated: RecordedRepair;
  journey_repair_action: RecordedAction;
  journey_replay: ReplayResult;
}
