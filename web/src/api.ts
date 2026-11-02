/* Experiment API seam for the guided UI.
 *
 * FixtureExperimentApi serves approved/illustrative fixtures while Tracks A-C
 * build the service. It still re-validates every submitted config with the
 * same rules the server will enforce, and it records the submitted config in
 * the evidence, so every UI control already flows end to end.
 *
 * LiveExperimentApi calls the real service routes (src/ghostlab/api) over
 * HTTP and executes submitted configs; fixture mode remains for offline dev
 * and tests only, never as a production fallback. */

import {
  ADAPTER_VERSION,
  WORLD_VERSION,
  canonicalize,
  validateEffectiveConfig,
  validateManifestShape,
  type AgentKind,
  type EffectiveConfig,
} from "./config";
import type {
  CapsuleV1,
  Divergence,
  ExperimentResult,
  InvariantFinding,
  RunResult,
} from "../../schemas/typescript/contracts";
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

/* Live E4 HTTP client: every method calls the real service routes.
 * Project scoping uses a fixed browser project id; the service still
 * validates it like any other caller. runExperiment executes the submitted
 * fixture live (submitted variant plus canonical baseline/faulted/repaired
 * companions) and builds the evidence from executed traces only. */

const LIVE_LABEL = "Live service: executed by the E4 API; evidence from this run.";
const LIVE_PROJECT_ID = "browser-guide";
const POLL_MS = 200;
const RUN_TIMEOUT_MS = 30000;

function isLiveRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function liveRequest(baseUrl: string, path: string, options: { method?: string; body?: unknown }): Promise<unknown> {
  const headers: Record<string, string> = { "X-GhostLab-Project": LIVE_PROJECT_ID };
  let body: string | undefined;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  const init: RequestInit = { method: options.method ?? "GET", headers };
  if (body !== undefined) init.body = body;
  const response = await fetch(`${baseUrl}${path}`, init);
  const text = await response.text();
  let data: unknown = null;
  try {
    data = text === "" ? null : (JSON.parse(text) as unknown);
  } catch {
    throw new Error(`live service at ${baseUrl} returned non-JSON for ${path}`);
  }
  if (!response.ok) {
    const detail = isLiveRecord(data) && typeof data["detail"] === "string" ? (data["detail"] as string) : text;
    throw new Error(`live service ${String(response.status)} for ${path}: ${detail}`);
  }
  return data;
}

function liveString(record: Record<string, unknown>, key: string, where: string): string {
  const value: unknown = record[key];
  if (typeof value !== "string") throw new Error(`live service returned malformed ${where}.${key}`);
  return value;
}

function liveRunResult(serverRecord: unknown, adapter: AgentKind): { run: RunResult; runId: string } {
  if (!isLiveRecord(serverRecord)) throw new Error("live service returned a malformed run record");
  const runId = liveString(serverRecord, "run_id", "run");
  const inner: unknown = serverRecord["run"];
  if (!isLiveRecord(inner)) throw new Error("live service returned a malformed run body");
  const status: unknown = inner["status"];
  if (status !== "completed" && status !== "budget_exhausted") {
    throw new Error(`live run ${runId} ended as ${String(status)}`);
  }
  const evidence: unknown = inner["evidence"];
  const checks: unknown = inner["checks"];
  if (!isLiveRecord(evidence) || !isLiveRecord(checks)) throw new Error("live service returned malformed run evidence");
  const snapshot: unknown = evidence["snapshot"];
  const trace: unknown = evidence["trace"];
  if (!isLiveRecord(snapshot) || !Array.isArray(trace)) throw new Error("live service returned malformed run evidence");
  const run = {
    actor_id: isLiveRecord(snapshot["order"]) && typeof snapshot["order"]["customer_id"] === "string"
      ? (snapshot["order"]["customer_id"] as string)
      : "",
    adapter,
    adapter_version: ADAPTER_VERSION,
    checks: checks as unknown as RunResult["checks"],
    final_state: snapshot as unknown as RunResult["final_state"],
    status: status as RunResult["status"],
    trace: trace as unknown as RunResult["trace"],
  } satisfies RunResult;
  return { run, runId };
}

export class LiveExperimentApi implements ExperimentApi {
  readonly mode: EvidenceMode = "live";
  private readonly baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
  }

  async listEnvs(): Promise<EnvSummary[]> {
    const data = await liveRequest(this.baseUrl, "/v1/envs", {});
    if (!isLiveRecord(data) || !Array.isArray(data["envs"])) throw new Error("live service returned malformed env list");
    return (data["envs"] as unknown[]).map((entry): EnvSummary => {
      if (!isLiveRecord(entry)) throw new Error("live service returned a malformed env entry");
      return {
        envId: liveString(entry, "env_id", "env"),
        envVersion: liveString(entry, "env_version", "env"),
        adapterRef: liveString(entry, "adapter_ref", "env"),
        displayName: liveString(entry, "display_name", "env"),
        status: liveString(entry, "status", "env"),
        label: LIVE_LABEL,
      };
    });
  }

  async getCapabilities(envId: string, manifestDigest: string | null): Promise<CapabilitiesView> {
    const query = manifestDigest === null ? "" : `?manifest_digest=${encodeURIComponent(manifestDigest)}`;
    const data = await liveRequest(this.baseUrl, `/v1/envs/${encodeURIComponent(envId)}/capabilities${query}`, {});
    if (!isLiveRecord(data)) throw new Error("live service returned malformed capabilities");
    const tools = (key: string): CapabilityToolList => {
      const value: unknown = data[key];
      if (!Array.isArray(value)) throw new Error(`live service returned malformed capabilities.${key}`);
      return value as unknown as CapabilityToolList;
    };
    const modes = (key: string): string[] => {
      const value: unknown = data[key];
      if (!Array.isArray(value) || !value.every((v) => typeof v === "string")) {
        throw new Error(`live service returned malformed capabilities.${key}`);
      }
      return [...(value as string[])];
    };
    const hooks: unknown = data["fault_hooks"];
    if (!Array.isArray(hooks) || !hooks.every((v) => typeof v === "string")) {
      throw new Error("live service returned malformed capabilities.fault_hooks");
    }
    const supports: unknown = data["supports_cancellation"];
    const match: unknown = data["match"];
    const mismatches: unknown = data["mismatches"];
    if (typeof supports !== "boolean" || typeof match !== "boolean" || !Array.isArray(mismatches)) {
      throw new Error("live service returned malformed capabilities flags");
    }
    const digest: unknown = data["manifest_digest"];
    return {
      envId: liveString(data, "env_id", "capabilities"),
      envVersion: liveString(data, "env_version", "capabilities"),
      adapterRef: liveString(data, "adapter_ref", "capabilities"),
      codeDigest: liveString(data, "code_digest", "capabilities"),
      manifestDigest: digest === null ? null : typeof digest === "string" ? digest : (() => { throw new Error("malformed manifest_digest"); })(),
      declaredTools: tools("declared_tools"),
      discoveredTools: tools("discovered_tools"),
      declaredModes: modes("declared_modes"),
      discoveredModes: modes("discovered_modes"),
      faultHooks: [...(hooks as string[])],
      supportsCancellation: supports,
      isolation: liveString(data, "isolation", "capabilities"),
      match,
      mismatches: (mismatches as unknown[]).map((v) => String(v)),
      label: LIVE_LABEL,
    };
  }

  async getQualification(envId: string): Promise<QualificationView> {
    const data = await liveRequest(this.baseUrl, `/v1/envs/${encodeURIComponent(envId)}/report`, {});
    if (!isLiveRecord(data) || !isLiveRecord(data["report"])) throw new Error("live service returned a malformed report");
    const report = data["report"];
    const checks: unknown = report["checks"];
    if (!Array.isArray(checks)) throw new Error("live service returned malformed report checks");
    const digest: unknown = report["manifest_digest"];
    return {
      schemaVersion: 1,
      envId: liveString(report, "env_id", "report"),
      envVersion: liveString(report, "env_version", "report"),
      adapterRef: liveString(report, "adapter_ref", "report"),
      codeDigest: liveString(report, "code_digest", "report"),
      manifestDigest: digest === null ? null : typeof digest === "string" ? digest : (() => { throw new Error("malformed manifest_digest"); })(),
      runnerVersion: liveString(report, "runner_version", "report"),
      checks: (checks as unknown[]).map((entry) => {
        if (!isLiveRecord(entry)) throw new Error("live service returned a malformed check");
        const passed: unknown = entry["passed"];
        if (typeof passed !== "boolean") throw new Error("live service returned a malformed check");
        return { name: liveString(entry, "name", "check"), passed, detail: liveString(entry, "detail", "check") };
      }),
      status: liveString(report, "status", "report"),
      createdAt: liveString(report, "created_at", "report"),
      label: LIVE_LABEL,
    };
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
    const data = await liveRequest(this.baseUrl, "/v1/envs/import-manifest", { method: "POST", body: { manifest: checked.manifest } });
    if (!isLiveRecord(data)) throw new Error("live service returned a malformed import result");
    const returnedEnv = liveString(data, "env_id", "import");
    if (returnedEnv !== envId) throw new Error("manifest belongs to a different env_id");
    return {
      manifestDigest: liveString(data, "manifest_digest", "import"),
      envId: returnedEnv,
      envVersion: liveString(data, "env_version", "import"),
      canonicalText: canonicalize(checked.manifest),
    };
  }

  private async submitAndRun(effective: EffectiveConfig): Promise<{ run: RunResult; runId: string }> {
    const submitted = await liveRequest(this.baseUrl, "/v1/guide/submit", { method: "POST", body: { effective_config: effective } });
    if (!isLiveRecord(submitted)) throw new Error("live service returned a malformed guide result");
    const resolvedId = liveString(submitted, "resolved_id", "guide");
    const started = await liveRequest(this.baseUrl, "/v1/runs", { method: "POST", body: { resolved_id: resolvedId } });
    if (!isLiveRecord(started)) throw new Error("live service returned a malformed run start");
    const groupId = liveString(started, "group_id", "run");
    const deadline = Date.now() + RUN_TIMEOUT_MS;
    for (;;) {
      const status = await liveRequest(this.baseUrl, `/v1/runs/${encodeURIComponent(groupId)}`, {});
      if (!isLiveRecord(status)) throw new Error("live service returned a malformed run status");
      const state: unknown = status["status"];
      if (state !== "running") {
        if (state === "failed") {
          const detail = typeof status["error"] === "string" ? (status["error"] as string) : "run failed";
          throw new Error(`live run failed: ${detail}`);
        }
        if (state === "cancelled") throw new Error("live run was cancelled");
        if (state !== "completed") throw new Error(`live run ended as ${String(state)}`);
        const runs: unknown = status["runs"];
        if (!Array.isArray(runs) || runs.length === 0) throw new Error("live run completed with no runs");
        const first: unknown = runs[0];
        return liveRunResult(first, effective.agent.adapter);
      }
      if (Date.now() > deadline) throw new Error("live run timed out waiting for completion");
      await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    }
  }

  async runExperiment(config: EffectiveConfig): Promise<ExperimentEvidence> {
    const checked = validateEffectiveConfig(JSON.parse(JSON.stringify(config)) as unknown);
    if (!checked.ok) {
      const details = checked.errors.map((e) => `${e.path}: ${e.message}`).join("; ");
      throw new Error(`config rejected by validation: ${details}`);
    }
    const submittedAt = new Date().toISOString();
    const variant = (timeoutAfterCommit: boolean, adapter: AgentKind): EffectiveConfig =>
      ({ ...copy(config), faults: { timeoutAfterCommit }, agent: { ...config.agent, adapter } });
    const baselineCfg = variant(false, "vulnerable");
    const faultedCfg = variant(true, "vulnerable");
    const repairedCfg = variant(true, "repaired");
    const submittedMatches =
      (config.faults.timeoutAfterCommit === false && config.agent.adapter === "vulnerable") ||
      (config.faults.timeoutAfterCommit === true && config.agent.adapter === "vulnerable") ||
      (config.faults.timeoutAfterCommit === true && config.agent.adapter === "repaired");
    const baseline = await this.submitAndRun(baselineCfg);
    const faulted = await this.submitAndRun(faultedCfg);
    const repaired = await this.submitAndRun(repairedCfg);
    let submittedNote: string;
    if (submittedMatches) {
      const key = config.agent.adapter === "repaired" ? "repaired" : config.faults.timeoutAfterCommit ? "faulted" : "baseline";
      submittedNote = `The submitted variant (${config.agent.adapter}, timeout_after_commit=${String(config.faults.timeoutAfterCommit)}) executed live as the ${key} branch; companions executed live for comparison.`;
    } else {
      const extra = await this.submitAndRun(copy(config));
      submittedNote =
        `The submitted variant (repaired, timeout_after_commit=false) matches no canonical branch, so it was executed live separately (run ${extra.runId}, total ${String(extra.run.final_state.total_refunded_minor)} minor) alongside the canonical live branches.`;
    }
    const firstDivergence: Divergence | null = (() => {
      const maxLen = Math.max(baseline.run.trace.length, faulted.run.trace.length);
      for (let index = 0; index < maxLen; index += 1) {
        const left: unknown = baseline.run.trace[index] ?? null;
        const right: unknown = faulted.run.trace[index] ?? null;
        if (JSON.stringify(left) !== JSON.stringify(right)) {
          return {
            event_id: index + 1,
            baseline: (left as Divergence["baseline"]),
            faulted: (right as Divergence["faulted"]),
          };
        }
      }
      return null;
    })();
    const invariantOrder = ["authorized_refunds", "total_refund_within_payment", "no_duplicate_full_refund", "unique_idempotency_keys"] as const;
    const violated = invariantOrder.filter((name) => faulted.run.checks[name] !== true);
    const result: ExperimentResult = {
      baseline: baseline.run,
      faulted: faulted.run,
      repaired: repaired.run,
      diff: {
        baseline_refunded_minor: baseline.run.final_state.total_refunded_minor,
        faulted_refunded_minor: faulted.run.final_state.total_refunded_minor,
        first_divergence: firstDivergence,
        violated_invariants: [...violated],
      },
    };
    const payload = {
      actor_id: faulted.run.actor_id,
      fixture: {
        currency: config.fixture.currency,
        customer_id: config.fixture.customerId,
        order_id: config.fixture.orderId,
        paid_minor: config.fixture.paidMinor,
      },
      recorded_model_outputs: [],
      run: faulted.run,
      scenario: { max_tool_calls: config.limits.maxToolCalls, timeout_after_commit: true },
      schema_version: 1,
      seed: 0,
      source: "trusted-synthetic-refund-adapter",
      world_version: WORLD_VERSION,
    };
    const capsule: CapsuleV1 = {
      payload: payload as unknown as CapsuleV1["payload"],
      sha256: await sha256Hex(canonicalize(payload)),
    };
    const failedName = invariantOrder.find((name) => faulted.run.checks[name] !== true);
    const focusFirst: string | undefined = config.checks.invariants[0];
    const findingInvariant = failedName ?? ((focusFirst as typeof invariantOrder[number] | undefined) ?? "total_refund_within_payment");
    const verdict = failedName === undefined ? "passed" : "failed";
    const paidMinor = faulted.run.final_state.order.paid_minor;
    const totalMinor = faulted.run.final_state.total_refunded_minor;
    const fullCount = faulted.run.final_state.ledger.filter((row) => row.amount_minor === paidMinor).length;
    const finding: InvariantFinding = {
      evidence_event_ids: faulted.run.trace.map((event) => event.event_id),
      finding_id: `finding-${faulted.runId}`,
      invariant: findingInvariant,
      limit_minor: findingInvariant === "total_refund_within_payment" ? paidMinor : findingInvariant === "no_duplicate_full_refund" ? 1 : null,
      observed_minor: findingInvariant === "total_refund_within_payment" ? totalMinor : findingInvariant === "no_duplicate_full_refund" ? fullCount : null,
      recorded_at: submittedAt,
      run_id: faulted.runId,
      schema_version: 1,
      verdict,
      verifier_version: "refund-verifier-1",
    };
    return {
      mode: "live",
      submittedAt,
      effectiveConfig: copy(config),
      source: { experimentResult: LIVE_LABEL, capsule: LIVE_LABEL, env: LIVE_LABEL },
      result,
      capsule,
      finding,
      notices: [
        "Live execution: every total, trace and verdict below comes from the E4 service running this fixture, not from fixtures.",
        submittedNote,
        "Zero model calls; no credentials were used, requested or stored.",
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
      "GhostLab guided-run evidence bundle (live E4 service).",
      "",
      `mode: ${evidence.mode}`,
      `submittedAt: ${evidence.submittedAt}`,
      "",
      "Files:",
      "- effective-config.json: the exact config the UI submitted and the run recorded.",
      "- evidence.json: recorded config plus the executed result summary.",
      "- capsule-live.json: live faulted-branch capsule (payload and sha256) from executed evidence.",
      "",
      `run results: ${evidence.source.experimentResult}`,
      `capsule: ${evidence.source.capsule}`,
      `env data: ${evidence.source.env}`,
      "",
      "Retest this exact config (real execution, zero model calls):",
      "  POST the effective config to /v1/guide/submit, then POST the",
      "  resolved_id to /v1/runs and poll the group to completion.",
      "",
      "No credentials are present in this bundle.",
      "",
    ].join("\n");
    return {
      files: [
        { name: "effective-config.json", mediaType: "application/json", content: `${JSON.stringify(evidence.effectiveConfig, null, 2)}\n` },
        { name: "evidence.json", mediaType: "application/json", content: `${JSON.stringify(evidenceDoc, null, 2)}\n` },
        { name: "capsule-live.json", mediaType: "application/json", content: `${JSON.stringify({ payload: evidence.capsule.payload, sha256: evidence.capsule.sha256 }, null, 2)}\n` },
        { name: "README.txt", mediaType: "text/plain", content: readme },
      ],
    };
  }
}

type CapabilityToolList = CapabilitiesView["declaredTools"];
