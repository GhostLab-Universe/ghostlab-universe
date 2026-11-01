import type { SessionInfo, WorldCampaignSpec } from "../api/api.generated.ts";

/**
 * The one-click demo: the scripted refund-world scenario, start to verdict. The
 * order is the synthetic INR 2,500 one from the published refund demo: a payment
 * commits, its response times out, and a vulnerable retry refunds twice.
 */
export const GUIDED_DEMO_WORLD: WorldCampaignSpec["world_version"] = "refund-world/1";
export const GUIDED_DEMO_SCENARIO = "default";
export const GUIDED_DEMO_OBJECTIVE = "Show the double refund after a post-commit timeout on a synthetic INR 2,500 order";
export const GUIDED_DEMO_CRITERION = "The verifier reproduces the over-refund from the recorded ledger";

/** M00's published local-demo limits. The service still applies its own ceilings. */
export const GUIDED_DEMO_BUDGET: WorldCampaignSpec["budget"] = {
  cost_currency: null,
  max_cost_minor: null,
  max_input_tokens: 200000,
  max_model_calls: 20,
  max_output_tokens: 20000,
  max_parallel_runs: 2,
  max_tool_calls: 60,
  max_wall_clock_ms: 600000,
};

export function guidedDemoSpec(): WorldCampaignSpec {
  return {
    schema_version: 1,
    world_version: GUIDED_DEMO_WORLD,
    objective: GUIDED_DEMO_OBJECTIVE,
    success_criteria: [GUIDED_DEMO_CRITERION],
    budget: GUIDED_DEMO_BUDGET,
  };
}

/** The demo needs the refund world among the worlds this session can start. */
export function guidedDemoAvailable(runnableWorlds: readonly string[]): boolean {
  return runnableWorlds.includes(GUIDED_DEMO_WORLD);
}

export const GUIDED_DEMO_STEPS: readonly string[] = [
  "Start the scripted refund-world scenario on synthetic data",
  "Watch campaign progress while the baseline and fork run",
  "Read the Reality Diff: first divergence, violation evidence and ledger",
  "Decide the repair in Approval and read the recorded verdict",
];

/** What this service's proposals would be. Scripted replies are never a model. */
export function guidedDemoInferenceNote(inference: SessionInfo["inference"]): string {
  if (inference === "fixture" || inference === "scripted") return "Proposals scripted: checked-in replies, not a model";
  if (inference === "disabled") return "Inference is disabled on this service";
  if (inference === "unrecorded") return "Inference not recorded for this service";
  return `inference ${inference}`;
}
