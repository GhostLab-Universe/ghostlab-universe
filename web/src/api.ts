/* Experiment API seam for the guided UI.
 *
 * FixtureExperimentApi serves approved/illustrative fixtures while Tracks A-C
 * build the service. It still re-validates every submitted config with the
 * same rules the server will enforce, and it records the submitted config in
 * the evidence, so every UI control already flows end to end.
 *
 * LiveExperimentApi is an honest stub: M07 routes do not exist yet, and this
 * module refuses to invent their contract. Wire it when M07 lands. */

import {
  canonicalize,
  validateEffectiveConfig,
  validateManifestShape,
  type EffectiveConfig,
} from "./config";
import {
  ILLUSTRATIVE_CAPABILITIES,
  ILLUSTRATIVE_ENVS,
  ILLUSTRATIVE_QUALIFICATION,
  capsuleFixture,
  experimentResultFixture,
  invariantFindingFixture,
} from "./fixtures";
import {
  APPROVED_FIXTURE_LABEL,
  ILLUSTRATIVE_FIXTURE_LABEL,
  SCRIPTED_RUN_LABEL,
  type CapabilitiesView,
  type EnvSummary,
  type EvidenceMode,
  type ExperimentEvidence,
  type ExportBundle,
  type ManifestImportResult,
  type QualificationView,
} from "./types";

export interface ExperimentApi {
  readonly mode: EvidenceMode;
  listEnvs(): Promise<EnvSummary[]>;
  getCapabilities(envId: string, manifestDigest: string | null): Promise<CapabilitiesView>;
  getQualification(envId: string): Promise<QualificationView>;
  importManifest(envId: string, manifestText: string): Promise<ManifestImportResult>;
  runExperiment(config: EffectiveConfig): Promise<ExperimentEvidence>;
  exportBundle(evidence: ExperimentEvidence): ExportBundle;
}

function copy<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

async function sha256Hex(text: string): Promise<string> {
  const subtle = globalThis.crypto?.subtle;
  if (subtle === undefined) {
    throw new Error("SHA-256 needs a secure browser context; cannot digest the manifest.");
  }
  const hash = await subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(hash)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export class FixtureExperimentApi implements ExperimentApi {
  readonly mode: EvidenceMode = "fixture-scripted";

  async listEnvs(): Promise<EnvSummary[]> {
    return copy([...ILLUSTRATIVE_ENVS]);
  }

  async getCapabilities(envId: string, manifestDigest: string | null): Promise<CapabilitiesView> {
    if (envId !== ILLUSTRATIVE_CAPABILITIES.envId) throw new Error(`unknown env_id '${envId}'`);
    return { ...copy(ILLUSTRATIVE_CAPABILITIES), manifestDigest };
  }

  async getQualification(envId: string): Promise<QualificationView> {
    if (envId !== ILLUSTRATIVE_QUALIFICATION.envId) throw new Error(`unknown env_id '${envId}'`);
    return copy(ILLUSTRATIVE_QUALIFICATION);
  }

  async importManifest(envId: string, manifestText: string): Promise<ManifestImportResult> {
    let parsed: unknown;
    try {
      parsed = JSON.parse(manifestText) as unknown;
    } catch {
      throw new Error("manifest is not valid JSON");
    }
    const checked = validateManifestShape(parsed);
    if (!checked.ok) {
      const details = checked.errors.map((e) => `${e.path}: ${e.message}`).join("; ");
      throw new Error(`manifest rejected: ${details}`);
    }
    const manifest = checked.manifest;
    if (manifest.env_id !== envId) throw new Error("manifest belongs to a different env_id");
    const known = ILLUSTRATIVE_ENVS.find((e) => e.envId === envId);
    if (known === undefined) throw new Error(`unknown env_id '${envId}'`);
    if (manifest.env_version !== known.envVersion || manifest.adapter_ref !== known.adapterRef) {
      throw new Error("manifest does not match the registration");
    }
    const canonicalText = canonicalize(manifest);
    const manifestDigest = await sha256Hex(canonicalText);
    return { manifestDigest, envId, envVersion: manifest.env_version, canonicalText };
  }

  async runExperiment(config: EffectiveConfig): Promise<ExperimentEvidence> {
    // Server-side stand-in: re-validate exactly what the UI validated.
    const checked = validateEffectiveConfig(JSON.parse(JSON.stringify(config)) as unknown);
    if (!checked.ok) {
      const details = checked.errors.map((e) => `${e.path}: ${e.message}`).join("; ");
      throw new Error(`config rejected by validation: ${details}`);
    }
    return {
      mode: "fixture-scripted",
      submittedAt: new Date().toISOString(),
      effectiveConfig: copy(config),
      source: {
        experimentResult: APPROVED_FIXTURE_LABEL,
        capsule: APPROVED_FIXTURE_LABEL,
        env: ILLUSTRATIVE_FIXTURE_LABEL,
      },
      result: copy(experimentResultFixture),
      capsule: copy(capsuleFixture),
      finding: copy(invariantFindingFixture),
      notices: [
        SCRIPTED_RUN_LABEL,
        "Totals, traces and verdicts below come from the approved M00 fixture, not from executing this config.",
        "No credentials were used, requested or stored at any step.",
      ],
    };
  }

  exportBundle(evidence: ExperimentEvidence): ExportBundle {
    const result = evidence.result;
    const divergence = result.diff.first_divergence;
    const evidenceDoc = {
      mode: evidence.mode,
      submittedAt: evidence.submittedAt,
      source: evidence.source,
      notices: evidence.notices,
      effectiveConfig: evidence.effectiveConfig,
      totalsMinor: {
        baseline: result.baseline.final_state.total_refunded_minor,
        faulted: result.faulted.final_state.total_refunded_minor,
        repaired: result.repaired.final_state.total_refunded_minor,
      },
      firstDivergenceEventId: divergence === null ? null : divergence.event_id,
      violatedInvariants: result.diff.violated_invariants,
      faultedChecks: result.faulted.checks,
      finding: evidence.finding,
    };
    const readme = [
      "GhostLab guided-run evidence bundle (Track D).",
      "",
      `mode: ${evidence.mode}`,
      `submittedAt: ${evidence.submittedAt}`,
      "",
      "Files:",
      "- effective-config.json: the exact config the UI submitted and the run recorded.",
      "- evidence.json: recorded config plus the rendered result summary.",
      "- capsule-fixture.json: re-serialization of the approved M00 fixture capsule",
      "  (payload and checksum identical; envelope whitespace differs). This is the",
      "  approved fixture for replay tooling, NOT output of this run.",
      "",
      `run results: ${evidence.source.experimentResult}`,
      `capsule: ${evidence.source.capsule}`,
      `env data: ${evidence.source.env}`,
      "",
      "Retest with the approved capsule (real replay, zero model calls):",
      "  python -m ghostlab replay examples/contracts/core/capsule_v1.refund_failure.json",
      "with PYTHONPATH=src. See docs/modules/M08.md.",
      "",
      "No credentials are present in this bundle.",
      "",
    ].join("\n");
    return {
      files: [
        { name: "effective-config.json", mediaType: "application/json", content: `${JSON.stringify(evidence.effectiveConfig, null, 2)}\n` },
        { name: "evidence.json", mediaType: "application/json", content: `${JSON.stringify(evidenceDoc, null, 2)}\n` },
        { name: "capsule-fixture.json", mediaType: "application/json", content: `${JSON.stringify({ payload: evidence.capsule.payload, sha256: evidence.capsule.sha256 }, null, 2)}\n` },
        { name: "README.txt", mediaType: "text/plain", content: readme },
      ],
    };
  }
}

/** Placeholder for the future M07 HTTP client. Throws; never invents routes. */
export class LiveExperimentApi implements ExperimentApi {
  readonly mode: EvidenceMode = "live";
  private readonly baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl;
  }

  private missing(): never {
    throw new Error(
      `Live API at ${this.baseUrl} is not integrated: M07 service routes are pending, ` +
        "so this client refuses to guess them. Fixture mode remains active.",
    );
  }

  async listEnvs(): Promise<EnvSummary[]> {
    this.missing();
  }

  async getCapabilities(): Promise<CapabilitiesView> {
    this.missing();
  }

  async getQualification(): Promise<QualificationView> {
    this.missing();
  }

  async importManifest(): Promise<ManifestImportResult> {
    this.missing();
  }

  async runExperiment(): Promise<ExperimentEvidence> {
    this.missing();
  }

  exportBundle(): ExportBundle {
    this.missing();
  }
}
