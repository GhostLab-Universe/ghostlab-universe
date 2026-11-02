/* Fixtures for Track D UI development. Two tiers, never mixed silently:
 * - Approved M00 fixtures (examples/contracts/**): validated against schemas
 *   by tests/contracts/test_contracts.py. Rendered only with a fixture label.
 * - Illustrative env fixtures below: hand-shaped from the inspected Track A
 *   return shapes (discover()/run_qualification()/import_manifest digests).
 *   They preview UI states for offline UI development; never evidence. */

import budgetLimitsRaw from "../../examples/contracts/budget_limits.local_demo.json";
import campaignSpecRaw from "../../examples/contracts/campaign_spec.refund_timeout.json";
import errorInfoRaw from "../../examples/contracts/error_info.committed_timeout.json";
import experimentProposalRaw from "../../examples/contracts/experiment_proposal.timeout_after_commit.json";
import invariantFindingRaw from "../../examples/contracts/invariant_finding.over_refund.json";
import capsuleRaw from "../../examples/contracts/core/capsule_v1.refund_failure.json";
import experimentResultRaw from "../../examples/contracts/core/experiment_result.refund_demo.json";
import illustrativeManifestJson from "./fixtures/illustrative-manifest.json";
import type {
  BudgetLimits,
  CampaignSpec,
  CapsuleV1,
  ErrorInfo,
  ExperimentProposal,
  ExperimentResult,
  InvariantFinding,
} from "../../schemas/typescript/contracts";
import {
  ILLUSTRATIVE_FIXTURE_LABEL,
  type CapabilitiesView,
  type EnvSummary,
  type QualificationView,
} from "./types";

/* Approved M00 fixtures. Casts are backed by the M00 contract tests, which
 * validate these exact files against the frozen schemas. */
export const campaignSpecExample = campaignSpecRaw as unknown as CampaignSpec;
export const experimentProposalExample = experimentProposalRaw as unknown as ExperimentProposal;
export const budgetLimitsExample = budgetLimitsRaw as unknown as BudgetLimits;
export const errorInfoExample = errorInfoRaw as unknown as ErrorInfo;
export const invariantFindingFixture = invariantFindingRaw as unknown as InvariantFinding;
export const experimentResultFixture = experimentResultRaw as unknown as ExperimentResult;
export const capsuleFixture = capsuleRaw as unknown as CapsuleV1;

export const ILLUSTRATIVE_MANIFEST_TEXT = JSON.stringify(illustrativeManifestJson, null, 2);

/* Illustrative env data. Shapes mirror the inspected Track A outputs; values
 * are placeholders. Displayed only beside ILLUSTRATIVE_FIXTURE_LABEL. */

export const ILLUSTRATIVE_ENVS: readonly EnvSummary[] = [
  {
    envId: "refund-env",
    envVersion: "refund-env/1",
    adapterRef: "ghostlab.envs.adapters:RefundEnvAdapter",
    displayName: "Synthetic refund",
    status: "not-qualified",
    label: ILLUSTRATIVE_FIXTURE_LABEL,
  },
];

const REFUND_TOOLS = [
  { name: "get_order", parameters: ["order_id", "customer_id"] },
  { name: "get_refund_status", parameters: ["order_id", "customer_id", "idempotency_key"] },
  { name: "issue_refund", parameters: ["order_id", "customer_id", "amount_minor", "idempotency_key"] },
];

export const ILLUSTRATIVE_CAPABILITIES: CapabilitiesView = {
  envId: "refund-env",
  envVersion: "refund-env/1",
  adapterRef: "ghostlab.envs.adapters:RefundEnvAdapter",
  codeDigest: "0".repeat(64),
  manifestDigest: null,
  declaredTools: REFUND_TOOLS,
  discoveredTools: REFUND_TOOLS,
  declaredModes: ["recorded-actions", "deterministic-reset"],
  discoveredModes: ["recorded-actions", "deterministic-reset"],
  faultHooks: ["timeout_after_commit"],
  supportsCancellation: true,
  isolation: "in-process-checked-in-local",
  match: true,
  mismatches: [],
  label: ILLUSTRATIVE_FIXTURE_LABEL,
};

export const ILLUSTRATIVE_QUALIFICATION: QualificationView = {
  schemaVersion: 1,
  envId: "refund-env",
  envVersion: "refund-env/1",
  adapterRef: "ghostlab.envs.adapters:RefundEnvAdapter",
  codeDigest: "0".repeat(64),
  manifestDigest: null,
  runnerVersion: "env-qualification/1",
  checks: [
    { name: "reset", passed: true, detail: "illustrative: reset reproduces the initial snapshot" },
    { name: "isolation", passed: true, detail: "illustrative: sibling untouched; own trace owned" },
    { name: "schemas", passed: true, detail: "illustrative: unknown/malformed rejected unmutated" },
    { name: "evidence", passed: true, detail: "illustrative: trace ordered; snapshot consistent" },
    { name: "fault-hooks", passed: true, detail: "illustrative: hooks set/clear cleanly" },
    { name: "cancellation", passed: true, detail: "illustrative: cancel raised; uncancelled works" },
    { name: "reproduction-modes", passed: true, detail: "illustrative: modes reproduced" },
  ],
  status: "qualified-for-declared",
  createdAt: "2026-11-02T00:00:00Z",
  label: ILLUSTRATIVE_FIXTURE_LABEL,
};
