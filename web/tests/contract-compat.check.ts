/**
 * Compile-time proof that the types generated from M07's OpenAPI document and M00's
 * generated contracts agree, in both directions, for every contract the UI renders.
 * A drift between the two fails `tsc -p tsconfig.tests.json`. Nothing here runs.
 */
import type * as Api from "../src/api/api.generated.ts";
import type * as M00 from "../../schemas/typescript/contracts.ts";

type Same<A, B> = [A] extends [B] ? ([B] extends [A] ? true : never) : never;

export const contractsAgree: [
  Same<Api.ApprovalDecision, M00.ApprovalDecision>,
  Same<Api.ApprovalRequest, M00.ApprovalRequest>,
  Same<Api.ApprovalSubmission, M00.ApprovalSubmission>,
  Same<Api.BudgetLimits, M00.BudgetLimits>,
  Same<Api.BudgetUsage, M00.BudgetUsage>,
  Same<Api.CapsuleV1, M00.CapsuleV1>,
  Same<Api.CapsuleV2, M00.CapsuleV2>,
  Same<Api.ErrorInfo, M00.ErrorInfo>,
  Same<Api.ExperimentEvent, M00.ExperimentEvent>,
  Same<Api.Ref, M00.Ref>,
  Same<Api.TaskSpec, M00.TaskSpec>,
  Same<Api.TrustedScope, M00.TrustedScope>,
  Same<Api.WorldCampaignRecord, M00.WorldCampaignRecord>,
  Same<Api.WorldCampaignSpec, M00.WorldCampaignSpec>,
  Same<Api.WorldExperimentProposal, M00.WorldExperimentProposal>,
  Same<Api.WorldInvariantFinding, M00.WorldInvariantFinding>,
  Same<Api.WorldRealityDiff, M00.WorldRealityDiff>,
  Same<Api.WorldRunResult, M00.WorldRunResult>,
] = [true, true, true, true, true, true, true, true, true, true, true, true, true, true, true, true, true, true];
