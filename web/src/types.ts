/* Shared view types for the Track D guided UI (type-only + constants).
 * Contract-backed fixtures are typed with the M00 generated types; the M00
 * contract tests (not this UI) validate those fixture files against schemas. */

import type {
  CapsuleV1,
  ExperimentResult,
  InvariantFinding,
} from "../../schemas/typescript/contracts";
import type { EffectiveConfig } from "./config";

export type EvidenceMode = "fixture-scripted" | "live";

/** Fixture provenance labels. Every fixture-rendered section shows one. */
export const APPROVED_FIXTURE_LABEL =
  "Approved M00 fixture: development aid, not evidence from this run.";
export const ILLUSTRATIVE_FIXTURE_LABEL =
  "Illustrative dev aid: placeholder until the Track A service lands; never evidence.";
export const SCRIPTED_RUN_LABEL =
  "Scripted run: the submitted config is recorded, but results are fixtures, not live execution.";

export interface EnvSummary {
  envId: string;
  envVersion: string;
  adapterRef: string;
  displayName: string;
  status: string;
  label: string;
}

export interface CapabilityTool {
  name: string;
  parameters: string[];
}

export interface CapabilitiesView {
  envId: string;
  envVersion: string;
  adapterRef: string;
  codeDigest: string;
  manifestDigest: string | null;
  declaredTools: CapabilityTool[];
  discoveredTools: CapabilityTool[];
  declaredModes: string[];
  discoveredModes: string[];
  faultHooks: string[];
  supportsCancellation: boolean;
  isolation: string;
  match: boolean;
  mismatches: string[];
  label: string;
}

export interface QualificationCheckView {
  name: string;
  passed: boolean;
  detail: string;
}

export interface QualificationView {
  schemaVersion: 1;
  envId: string;
  envVersion: string;
  adapterRef: string;
  codeDigest: string;
  manifestDigest: string | null;
  runnerVersion: string;
  checks: QualificationCheckView[];
  status: string;
  createdAt: string;
  label: string;
}

export interface ManifestImportResult {
  manifestDigest: string;
  envId: string;
  envVersion: string;
  canonicalText: string;
}

export interface ExperimentEvidence {
  mode: EvidenceMode;
  submittedAt: string;
  effectiveConfig: EffectiveConfig;
  /** Where each rendered section came from; displayed with the section. */
  source: {
    experimentResult: string;
    capsule: string;
    env: string;
  };
  result: ExperimentResult;
  capsule: CapsuleV1;
  finding: InvariantFinding;
  notices: string[];
}

export interface ExportFile {
  name: string;
  mediaType: string;
  content: string;
}

export interface ExportBundle {
  files: ExportFile[];
}
