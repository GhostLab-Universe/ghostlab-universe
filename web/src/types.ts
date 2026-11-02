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

/** One executed arm behind a guided run: the service-issued references.
 * Every id below was returned by the service for this run; the UI never
 * mints run, group, resolved or finding ids. */
export interface ArmReference {
  /** Canonical branch, or the extra submitted variant when it matches none. */
  variant: "baseline" | "faulted" | "repaired" | "submitted-extra";
  requestedId: string;
  resolvedId: string;
  groupId: string;
  runId: string;
  /** Controller run status: completed | budget_exhausted | cancelled | error. */
  status: string;
  /** Controller verdict: pass | fail | incomplete. */
  verdict: string;
  /** model_calls recorded by the controller for this run (0 when deterministic). */
  modelCalls: number;
  toolCalls: number;
  wallClockMs: number;
  /** Store digest of the run record; recovery re-verifies it live. */
  runDigest: string;
}

/** One effective→requested mapping note, echoed by POST /v1/guide/submit. */
export interface MappingNote {
  effective_path: string;
  requested_path: string | null;
  action: string;
  detail: string;
}

/** Pinned versions bound by resolution, echoed by the service. */
export interface ResolvedPinsView {
  envId: string;
  envVersion: string;
  adapterRef: string;
  codeDigest: string;
  manifestDigest: string | null;
  worldVersion: string;
  adapterVersion: string;
  controllerVersion: string;
}

/** Server-derived provenance. Every label renders from observed service
 * data (pins + run bodies), never from hardcoded claims. */
export interface EvidenceProvenance {
  /** e.g. local deterministic adapter + world version. */
  environment: string;
  /** deterministic-adapter (verified, 0 model calls) or model-backed. */
  target: string;
  /** The guided run drives a fixed adapter procedure; no explorer runs. */
  explorer: string;
  /** executed (fresh run bodies) or historical (recovered + digest-verified). */
  evidence: string;
  /** Sum of controller-recorded model_calls across arms. */
  modelCalls: number;
  /** Spending statement derived from the contract (no model/cost path). */
  spend: string;
}

/** Service-issued references + resolution echo attached to live evidence. */
export interface ServiceReferences {
  projectId: string;
  baseUrl: string;
  arms: ArmReference[];
  pins: ResolvedPinsView | null;
  mappingNotes: MappingNote[];
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
  /** Present on service evidence: ids the service returned for this run. */
  references?: ServiceReferences;
  /** Present on service evidence: labels derived from service responses. */
  provenance?: EvidenceProvenance;
}

export interface ExportFile {
  name: string;
  mediaType: string;
  content: string;
}

export interface ExportBundle {
  files: ExportFile[];
}
