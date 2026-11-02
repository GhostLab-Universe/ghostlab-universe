/* ServiceGuideApi: the guided UI's service-facing adapter.
 *
 * Transport ownership: ALL route calls for the flows LiveExperimentApi
 * implements (envs, capabilities, report, import-manifest, guide/submit,
 * runs, run polling) live in web/src/api.ts and stay there — this module
 * delegates to LiveExperimentApi and never re-implements those calls, so a
 * contract drift in those routes is a one-file fix in api.ts. This module
 * only calls the routes the transport does not: qualify, cancel, compare,
 * export and standalone group reads (reload recovery). If the parallel
 * track extends LiveExperimentApi to cover those, delete the gap calls here
 * and delegate instead.
 *
 * Reference capture: the transport returns assembled evidence but drops the
 * service-issued ids (requested/resolved/group/run). This module observes
 * the fetch traffic of each delegated call (scoped global-fetch wrapper,
 * restored afterwards) and records only what the service actually returned:
 * no extra requests, no invented ids, no duplicate execution.
 *
 * Honesty: provenance labels derive from observed pins + run bodies. The
 * diff/finding/capsule the transport assembles in the browser from executed
 * evidence are labeled as browser-assembled; server-issued finding/capsule
 * are not part of this contract. Backend failures surface as typed errors
 * and NEVER fall back to fixtures.
 *
 * Pure transport: type-only imports plus fetch; no React, no DOM except an
 * injected storage (defaults to a guarded sessionStorage). */

import type { LiveExperimentApi } from "./api";
import type { EffectiveConfig } from "./config";
import type {
  ArmReference,
  CapabilitiesView,
  EnvSummary,
  EvidenceMode,
  EvidenceProvenance,
  ExperimentEvidence,
  ManifestImportResult,
  MappingNote,
  QualificationView,
  ResolvedPinsView,
  ServiceReferences,
} from "./types";

export const SERVICE_ROUTES_VERSION = "e4/1";
export const STORAGE_KEY = "gl.guide.runs.v1";
export const STORAGE_CAP = 8;
/** Project the browser client sends; confirmed from observed traffic. */
export const FALLBACK_PROJECT_ID = "browser-guide";
const TERMINAL_GROUP = ["completed", "cancelled", "failed"];

/** Minimal storage surface (sessionStorage in the app, fakes in tests). */
export interface StoredRunsStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function needString(node: Record<string, unknown>, key: string, where: string): string {
  const value: unknown = node[key];
  if (typeof value !== "string") throw new Error(`service returned malformed ${where}.${key}`);
  return value;
}

function needInt(node: Record<string, unknown>, key: string, where: string): number {
  const value: unknown = node[key];
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new Error(`service returned malformed ${where}.${key}`);
  }
  return value;
}

function needRecord(node: Record<string, unknown>, key: string, where: string): Record<string, unknown> {
  const value: unknown = node[key];
  if (!isRecord(value)) throw new Error(`service returned malformed ${where}.${key}`);
  return value;
}

/** One observed HTTP exchange (request + parsed response, if any). */
export interface ObservedCall {
  method: string;
  path: string;
  status: number | null;
  projectId: string | null;
  requestBody: unknown;
  responseBody: unknown;
}

/** Records fetch traffic without changing it. */
export class CallRecorder {
  readonly calls: ObservedCall[] = [];

  wrap(fetchImpl: typeof fetch): typeof fetch {
    const calls = this.calls;
    const recording: typeof fetch = (async (input: unknown, init?: unknown): Promise<Response> => {
      const url = typeof input === "string" ? input : String((input as { url?: unknown }).url ?? input);
      const options = (init ?? {}) as { method?: unknown; headers?: unknown; body?: unknown };
      const method = typeof options.method === "string" ? options.method : "GET";
      let projectId: string | null = null;
      const headers = options.headers;
      if (isRecord(headers)) {
        const found: unknown = headers["X-GhostLab-Project"];
        if (typeof found === "string") projectId = found;
      } else if (typeof Headers !== "undefined" && headers instanceof Headers) {
        projectId = headers.get("X-GhostLab-Project");
      }
      let requestBody: unknown = null;
      if (typeof options.body === "string" && options.body !== "") {
        try {
          requestBody = JSON.parse(options.body) as unknown;
        } catch {
          requestBody = null;
        }
      }
      const response = await fetchImpl(input as RequestInfo, init as RequestInit);
      const reply = response.clone();
      let responseBody: unknown = null;
      try {
        const text = await reply.text();
        responseBody = text === "" ? null : (JSON.parse(text) as unknown);
      } catch {
        responseBody = null;
      }
      calls.push({ method, path: url, status: response.status, projectId, requestBody, responseBody });
      return response;
    }) as typeof fetch;
    return recording;
  }
}

/** Run fn with global fetch wrapped by the recorder; always restores.
 * The restore is conditional so overlapping wrapped calls (capabilities +
 * report load concurrently) never clobber each other's wrapper. */
async function withRecording<T>(recorder: CallRecorder, fn: () => Promise<T>): Promise<T> {
  const realFetch = globalThis.fetch;
  const recording = recorder.wrap(realFetch);
  globalThis.fetch = recording;
  try {
    return await fn();
  } finally {
    if (globalThis.fetch === recording) globalThis.fetch = realFetch;
  }
}

function safeSessionStorage(): StoredRunsStorage | null {
  try {
    const candidate: unknown = globalThis.sessionStorage;
    if (candidate === undefined || candidate === null) return null;
    const storage = candidate as StoredRunsStorage;
    if (typeof storage.getItem !== "function") return null;
    return storage;
  } catch {
    return null;
  }
}

// Secret prescan mirrors the server markers (configs.scan_for_secrets):
// refusing client-side before submit matches the server refusal and keeps
// operator notes honest. Values are never echoed.
const SECRET_MARKERS = ["API_KEY", "APIKEY", "PRIVATE KEY", "client_secret", "BEARER ", "xox-"];

export function prescanEffective(config: EffectiveConfig): void {
  const walk = (node: unknown, path: string): void => {
    if (typeof node === "string") {
      for (const marker of SECRET_MARKERS) {
        if (node.includes(marker)) {
          throw new Error(
            `${path} must not carry secret-like text (marker '${marker.trim()}'); configs hold no secrets`,
          );
        }
      }
    } else if (Array.isArray(node)) {
      node.forEach((item, index) => walk(item, `${path}[${index}]`));
    } else if (isRecord(node)) {
      for (const key of Object.keys(node)) walk(node[key], `${path}.${key}`);
    }
  };
  walk(config, "effective_config");
}

export type FailureKind =
  | "connection"
  | "config"
  | "forbidden"
  | "not-found"
  | "budget"
  | "too-large"
  | "timeout"
  | "cancelled"
  | "internal";

export interface ServiceFailure {
  kind: FailureKind;
  status: number | null;
  detail: string;
  message: string;
}

/** Typed service failure. The config is always preserved for retry. */
export class ServiceGuideError extends Error {
  readonly kind: FailureKind;
  readonly status: number | null;
  readonly detail: string;

  constructor(failure: ServiceFailure) {
    super(failure.message);
    this.name = "ServiceGuideError";
    this.kind = failure.kind;
    this.status = failure.status;
    this.detail = failure.detail;
  }
}

const LIVE_MESSAGE = /^live service (\d+) for (\S+): ([\s\S]*)$/;

function kindForStatus(status: number): FailureKind {
  if (status === 400) return "config";
  if (status === 403) return "forbidden";
  if (status === 404) return "not-found";
  if (status === 409) return "budget";
  if (status === 413) return "too-large";
  return "internal";
}

/** Classify a transport/gap failure. Never throws, never invents data. */
export function toServiceFailure(error: unknown, baseUrl: string): ServiceFailure {
  const where = baseUrl === "" ? "same-origin service" : `service at ${baseUrl}`;
  if (error instanceof ServiceGuideError) {
    return { kind: error.kind, status: error.status, detail: error.detail, message: error.message };
  }
  const message = error instanceof Error ? error.message : String(error);
  if (error instanceof TypeError || /fetch failed|networkerror|failed to fetch|load failed/i.test(message)) {
    const cause = message.length > 160 ? `${message.slice(0, 160)}…` : message;
    const detail = `The ${where} is unreachable (cause: ${cause}). The config is preserved; fix the service and retry — no fixtures were substituted.`;
    return { kind: "connection", status: null, detail, message: detail };
  }
  const match = LIVE_MESSAGE.exec(message);
  if (match !== null) {
    const status = Number(match[1]);
    const path = String(match[2]);
    const detail = String(match[3]);
    const kind = kindForStatus(status);
    const lead =
      kind === "config"
        ? `The service rejected the request (400 ${path}). The config is preserved; fix it and retry.`
        : kind === "forbidden"
          ? `Cross-project access refused (403 ${path}).`
          : kind === "not-found"
            ? `Unknown id (404 ${path}). The record or report does not exist in this project.`
            : kind === "budget"
              ? `Budget exhausted (409 ${path}). The run is incomplete; the environment was not touched further.`
              : kind === "too-large"
                ? `Request too large (413 ${path}).`
                : `Service error (${String(status)} ${path}).`;
    return { kind, status, detail, message: `${lead} Detail: ${detail}` };
  }
  if (/timed out waiting for completion/.test(message)) {
    const detail =
      "The run did not finish within the client timeout. It may still complete server-side; stored group handles (step 6) recover it — nothing was fabricated.";
    return { kind: "timeout", status: null, detail, message: detail };
  }
  if (/was cancelled/.test(message)) {
    return { kind: "cancelled", status: null, detail: message, message: "The run was cancelled. Partial groups stay available for inspection." };
  }
  if (/non-JSON|malformed/.test(message)) {
    const detail = `The ${where} answered unexpectedly: ${message}`;
    return { kind: "internal", status: null, detail, message: detail };
  }
  return { kind: "internal", status: null, detail: message, message: `Service failure: ${message}` };
}

function fail(failure: ServiceFailure): never {
  throw new ServiceGuideError(failure);
}

/** One run record inside a group (Track C run body, service-issued). */
export interface RunRecordView {
  runId: string;
  resolvedId: string;
  repetition: number;
  attempt: number;
  digest: string;
  createdAt: string;
  status: string;
  verdict: string;
  modelCalls: number;
  toolCalls: number;
  wallClockMs: number;
  error: string | null;
  evidence: unknown;
  checks: unknown;
}

export interface RunGroupView {
  projectId: string;
  groupId: string;
  resolvedId: string;
  status: string;
  runs: RunRecordView[];
  error: string | null;
  createdAt: string;
  updatedAt: string;
}

export function parseRunRecord(node: unknown): RunRecordView {
  if (!isRecord(node)) throw new Error("service returned a malformed run record");
  const inner = needRecord(node, "run", "run");
  const error: unknown = inner["error"];
  const evidence: unknown = inner["evidence"];
  const checks: unknown = inner["checks"];
  return {
    runId: needString(node, "run_id", "run"),
    resolvedId: needString(node, "resolved_id", "run"),
    repetition: needInt(node, "repetition", "run"),
    attempt: needInt(node, "attempt", "run"),
    digest: needString(node, "digest", "run"),
    createdAt: needString(node, "created_at", "run"),
    status: needString(inner, "status", "run.run"),
    verdict: needString(inner, "verdict", "run.run"),
    modelCalls: needInt(inner, "model_calls", "run.run"),
    toolCalls: needInt(inner, "tool_calls", "run.run"),
    wallClockMs: needInt(inner, "wall_clock_ms", "run.run"),
    error: error === null ? null : typeof error === "string" ? error : String(error),
    evidence: evidence === undefined ? null : evidence,
    checks: checks === undefined ? null : checks,
  };
}

export function parseRunGroup(node: unknown): RunGroupView {
  if (!isRecord(node)) throw new Error("service returned a malformed run group");
  const runs: unknown = node["runs"];
  if (!Array.isArray(runs)) throw new Error("service returned a malformed run group.runs");
  const error: unknown = node["error"];
  return {
    projectId: needString(node, "project_id", "group"),
    groupId: needString(node, "group_id", "group"),
    resolvedId: needString(node, "resolved_id", "group"),
    status: needString(node, "status", "group"),
    runs: runs.map(parseRunRecord),
    error: error === null ? null : typeof error === "string" ? error : String(error),
    createdAt: needString(node, "created_at", "group"),
    updatedAt: needString(node, "updated_at", "group"),
  };
}

export function parseMappingNotes(node: unknown): MappingNote[] {
  if (!Array.isArray(node)) throw new Error("service returned malformed mapping_notes");
  return node.map((entry): MappingNote => {
    if (!isRecord(entry)) throw new Error("service returned a malformed mapping note");
    const requested: unknown = entry["requested_path"];
    return {
      effective_path: needString(entry, "effective_path", "mapping_note"),
      requested_path: requested === null ? null : typeof requested === "string" ? requested : String(requested),
      action: needString(entry, "action", "mapping_note"),
      detail: needString(entry, "detail", "mapping_note"),
    };
  });
}

export function parsePins(node: unknown): ResolvedPinsView {
  if (!isRecord(node)) throw new Error("service returned malformed resolved pins");
  const digest: unknown = node["manifest_digest"];
  return {
    envId: needString(node, "env_id", "pins"),
    envVersion: needString(node, "env_version", "pins"),
    adapterRef: needString(node, "adapter_ref", "pins"),
    codeDigest: needString(node, "code_digest", "pins"),
    manifestDigest: digest === null ? null : typeof digest === "string" ? digest : String(digest),
    worldVersion: needString(node, "world_version", "pins"),
    adapterVersion: needString(node, "adapter_version", "pins"),
    controllerVersion: needString(node, "controller_version", "pins"),
  };
}

interface ObservedSubmit {
  requestedId: string;
  resolvedId: string;
  mappingNotes: MappingNote[];
  pins: ResolvedPinsView;
  variant: ArmReference["variant"];
}

function variantOf(effective: unknown): ArmReference["variant"] {
  if (!isRecord(effective)) return "submitted-extra";
  const agent: unknown = effective["agent"];
  const faults: unknown = effective["faults"];
  const adapter = isRecord(agent) ? agent["adapter"] : null;
  const timeout = isRecord(faults) ? faults["timeoutAfterCommit"] : null;
  if (adapter === "vulnerable" && timeout === false) return "baseline";
  if (adapter === "vulnerable" && timeout === true) return "faulted";
  if (adapter === "repaired" && timeout === true) return "repaired";
  return "submitted-extra";
}

function pathOf(call: ObservedCall): string {
  const raw = call.path;
  const query = raw.indexOf("?");
  const noQuery = query === -1 ? raw : raw.slice(0, query);
  const marker = "/v1/";
  const at = noQuery.indexOf(marker);
  return at === -1 ? noQuery : noQuery.slice(at);
}

/** Assemble per-arm references from observed submit/start/poll traffic. */
export function assembleReferences(calls: readonly ObservedCall[]): {
  submits: ObservedSubmit[];
  starts: Array<{ resolvedId: string; groupId: string }>;
  groups: RunGroupView[];
} {
  const submits: ObservedSubmit[] = [];
  const starts: Array<{ resolvedId: string; groupId: string }> = [];
  const groupsById = new Map<string, RunGroupView>();
  for (const call of calls) {
    if (call.responseBody === null || call.status === null || call.status >= 400) continue;
    const path = pathOf(call);
    if (call.method === "POST" && path === "/v1/guide/submit" && isRecord(call.responseBody)) {
      const body = call.responseBody;
      const resolved = needRecord(body, "resolved", "guide.submit");
      const pins = parsePins(resolved["pins"]);
      const variant = isRecord(call.requestBody)
        ? variantOf((call.requestBody as Record<string, unknown>)["effective_config"])
        : "submitted-extra";
      submits.push({
        requestedId: needString(body, "requested_id", "guide.submit"),
        resolvedId: needString(body, "resolved_id", "guide.submit"),
        mappingNotes: parseMappingNotes(body["mapping_notes"]),
        pins,
        variant,
      });
    } else if (call.method === "POST" && path === "/v1/runs" && isRecord(call.responseBody)) {
      const body = call.responseBody;
      starts.push({
        resolvedId: needString(body, "resolved_id", "runs.start"),
        groupId: needString(body, "group_id", "runs.start"),
      });
    } else if (call.method === "GET" && path.startsWith("/v1/runs/") && !path.endsWith("/export")) {
      try {
        const group = parseRunGroup(call.responseBody);
        groupsById.set(group.groupId, group);
      } catch {
        // A mid-poll malformed body must not hide the completed one that
        // follows; the final assembly below requires complete data.
      }
    }
  }
  return { submits, starts, groups: [...groupsById.values()] };
}

export function buildArms(
  submits: readonly ObservedSubmit[],
  starts: ReadonlyArray<{ resolvedId: string; groupId: string }>,
  groups: readonly RunGroupView[],
): ArmReference[] {
  const submitByResolved = new Map(submits.map((s) => [s.resolvedId, s]));
  const groupById = new Map(groups.map((g) => [g.groupId, g]));
  const arms: ArmReference[] = [];
  for (const start of starts) {
    const submit = submitByResolved.get(start.resolvedId);
    const group = groupById.get(start.groupId);
    if (submit === undefined || group === undefined || group.runs.length === 0) continue;
    const first = group.runs[0];
    if (first === undefined) continue;
    arms.push({
      variant: submit.variant,
      requestedId: submit.requestedId,
      resolvedId: submit.resolvedId,
      groupId: group.groupId,
      runId: first.runId,
      status: first.status,
      verdict: first.verdict,
      modelCalls: first.modelCalls,
      toolCalls: first.toolCalls,
      wallClockMs: first.wallClockMs,
      runDigest: first.digest,
    });
  }
  const order = ["baseline", "faulted", "repaired", "submitted-extra"];
  arms.sort((a, b) => order.indexOf(a.variant) - order.indexOf(b.variant));
  return arms;
}

function shortId(id: string): string {
  return id.length <= 12 ? id : `${id.slice(0, 8)}…${id.slice(-4)}`;
}

export function shortDigest(digest: string): string {
  return digest.length <= 12 ? digest : digest.slice(0, 12);
}

/** Derive provenance labels from observed pins + run bodies. */
export function deriveProvenance(arms: readonly ArmReference[], pins: ResolvedPinsView | null): EvidenceProvenance {
  const modelCalls = arms.reduce((sum, arm) => sum + arm.modelCalls, 0);
  const environment =
    pins === null
      ? "unavailable (no resolution echo was observed)"
      : `local deterministic adapter ${pins.adapterRef} (${pins.adapterVersion}); world ${pins.worldVersion}; ` +
        `env ${pins.envId} ${pins.envVersion}; code ${shortDigest(pins.codeDigest)}`;
  const target =
    modelCalls === 0
      ? `deterministic-adapter (verified: the controller recorded 0 model calls across ${String(arms.length)} arm(s))`
      : `model-backed (the controller recorded ${String(modelCalls)} model call(s); provider unverified — treat as untrusted)`;
  return {
    environment,
    target,
    explorer: "none — the guided run drives a fixed adapter procedure (vulnerable/repaired); no explorer runs",
    evidence: "executed (fresh run bodies recorded by the controller for this run)",
    modelCalls,
    spend: "no paid calls: this contract has no model or cost path (model/token/cost limits are recorded metadata only); nothing was billed",
  };
}

export interface CompareResult {
  leftId: string;
  rightId: string;
  leftDigest: string;
  rightDigest: string;
  equal: boolean;
  diffCount: number;
  truncated: boolean;
  diffs: Array<{ path: string; left: unknown; right: unknown }>;
}

export function parseCompare(node: unknown): CompareResult {
  if (!isRecord(node)) throw new Error("service returned a malformed compare result");
  const diffs: unknown = node["diffs"];
  if (!Array.isArray(diffs)) throw new Error("service returned malformed compare diffs");
  const equal: unknown = node["equal"];
  const truncated: unknown = node["truncated"];
  if (typeof equal !== "boolean" || typeof truncated !== "boolean") {
    throw new Error("service returned a malformed compare result");
  }
  return {
    leftId: needString(node, "left_id", "compare"),
    rightId: needString(node, "right_id", "compare"),
    leftDigest: needString(node, "left_digest", "compare"),
    rightDigest: needString(node, "right_digest", "compare"),
    equal,
    diffCount: needInt(node, "diff_count", "compare"),
    truncated,
    diffs: diffs.map((entry) => {
      if (!isRecord(entry)) throw new Error("service returned a malformed compare diff");
      return { path: needString(entry, "path", "compare.diff"), left: entry["left"] ?? null, right: entry["right"] ?? null };
    }),
  };
}

/** GET /v1/runs/{group_id}/export bundle (actual recorded artifacts). */
export interface ServerExportBundle {
  projectId: string;
  groupId: string;
  resolvedId: string;
  requestedId: string;
  status: string;
  effectiveConfig: EffectiveConfig | null;
  resolved: unknown;
  runs: RunRecordView[];
  mappingNotes: MappingNote[] | null;
  digests: { effectiveDigest: string | null; resolvedDigest: string; runDigests: string[] };
  exportedAt: string;
  raw: unknown;
}

export function parseExport(node: unknown): ServerExportBundle {
  if (!isRecord(node)) throw new Error("service returned a malformed export bundle");
  const runs: unknown = node["runs"];
  if (!Array.isArray(runs)) throw new Error("service returned malformed export runs");
  const digests = needRecord(node, "digests", "export");
  const effectiveDigest: unknown = digests["effective_digest"];
  const runDigests: unknown = digests["run_digests"];
  if (!Array.isArray(runDigests) || !runDigests.every((d) => typeof d === "string")) {
    throw new Error("service returned malformed export digests");
  }
  const effective: unknown = node["effective_config"];
  const notes: unknown = node["mapping_notes"];
  return {
    projectId: needString(node, "project_id", "export"),
    groupId: needString(node, "group_id", "export"),
    resolvedId: needString(node, "resolved_id", "export"),
    requestedId: needString(node, "requested_id", "export"),
    status: needString(node, "status", "export"),
    effectiveConfig: effective === null ? null : (effective as EffectiveConfig),
    resolved: node["resolved"] ?? null,
    runs: runs.map(parseRunRecord),
    mappingNotes: notes === null ? null : parseMappingNotes(notes),
    digests: {
      effectiveDigest: effectiveDigest === null ? null : typeof effectiveDigest === "string" ? effectiveDigest : String(effectiveDigest),
      resolvedDigest: needString(digests, "resolved_digest", "export.digests"),
      runDigests: [...(runDigests as string[])],
    },
    exportedAt: needString(node, "exported_at", "export"),
    raw: node,
  };
}

/** Stored run handle for reload recovery (assembly + verification data). */
export interface StoredRun {
  version: 1;
  storedAt: string;
  submittedConfig: EffectiveConfig;
  evidence: ExperimentEvidence;
  arms: Array<{ variant: ArmReference["variant"]; groupId: string; resolvedId: string; runDigest: string }>;
}

export function loadStoredRuns(storage: StoredRunsStorage | null): StoredRun[] {
  if (storage === null) return [];
  let raw: string | null = null;
  try {
    raw = storage.getItem(STORAGE_KEY);
  } catch {
    return [];
  }
  if (raw === null || raw === "") return [];
  try {
    const parsed: unknown = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((entry): entry is StoredRun => isRecord(entry) && entry["version"] === 1) as StoredRun[];
  } catch {
    return [];
  }
}

export function saveStoredRun(storage: StoredRunsStorage | null, run: StoredRun): StoredRun[] {
  if (storage === null) return [];
  const kept = [...loadStoredRuns(storage), run].slice(-STORAGE_CAP);
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(kept));
  } catch {
    // Storage full or unavailable: recovery degrades, the run still stands.
  }
  return kept;
}

export function clearStoredRuns(storage: StoredRunsStorage | null): void {
  if (storage === null) return;
  try {
    storage.removeItem(STORAGE_KEY);
  } catch {
    // Best effort only.
  }
}

export interface ServiceGuideOptions {
  /** Transport that owns the core route calls (LiveExperimentApi). */
  live: LiveExperimentApi;
  /** Same-origin "" by default; ?api= override for dev/test. */
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  /** Gap-call timeout; the transport owns its own run timeout. */
  timeoutMs?: number;
  storage?: StoredRunsStorage | null;
}

/** Recovery verdict for one stored run. */
export interface RecoveryOutcome {
  stored: StoredRun;
  verified: boolean;
  problems: string[];
}

/**
 * Service-facing guide adapter. Delegates core flows to the transport,
 * captures service-issued references from the traffic, derives provenance
 * from observed data, and covers the routes the transport does not call
 * (qualify, cancel, compare, export, standalone group reads).
 */
export class ServiceGuideApi {
  readonly mode: EvidenceMode = "live";
  private readonly live: LiveExperimentApi;
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;
  private readonly storage: StoredRunsStorage | null;
  private observedProject: string | null = null;
  private running = false;
  private inflightGroups: string[] = [];

  constructor(options: ServiceGuideOptions) {
    this.live = options.live;
    const base = options.baseUrl ?? "";
    this.baseUrl = base.replace(/\/+$/, "");
    // Bound: invoking a captured window.fetch as a bare method throws
    // "Illegal invocation" in Chrome. Stubs are unaffected by binding.
    const captured = options.fetchImpl ?? globalThis.fetch;
    this.fetchImpl = captured.bind(globalThis) as typeof fetch;
    this.timeoutMs = options.timeoutMs ?? 30000;
    this.storage = options.storage === undefined ? safeSessionStorage() : options.storage;
  }

  get serviceBaseUrl(): string {
    return this.baseUrl;
  }

  get isRunning(): boolean {
    return this.running;
  }

  /** Project the transport sends (observed); fallback until first call. */
  projectId(): string {
    return this.observedProject ?? FALLBACK_PROJECT_ID;
  }

  projectObserved(): boolean {
    return this.observedProject !== null;
  }

  storedRuns(): StoredRun[] {
    return loadStoredRuns(this.storage);
  }

  forgetStoredRuns(): void {
    clearStoredRuns(this.storage);
  }

  // Delegated transport flows (route calls live in web/src/api.ts). -----

  async listEnvs(): Promise<EnvSummary[]> {
    return this.delegated(() => this.live.listEnvs());
  }

  async getCapabilities(envId: string, manifestDigest: string | null): Promise<CapabilitiesView> {
    return this.delegated(() => this.live.getCapabilities(envId, manifestDigest));
  }

  async getQualification(envId: string): Promise<QualificationView> {
    return this.delegated(() => this.live.getQualification(envId));
  }

  async importManifest(envId: string, manifestText: string): Promise<ManifestImportResult> {
    return this.delegated(() => this.live.importManifest(envId, manifestText));
  }

  private async delegated<T>(fn: () => Promise<T>): Promise<T> {
    const recorder = new CallRecorder();
    try {
      const out = await withRecording(recorder, fn);
      this.noteObserved(recorder.calls);
      return out;
    } catch (error) {
      this.noteObserved(recorder.calls);
      fail(toServiceFailure(error, this.baseUrl));
    }
  }

  private noteObserved(calls: readonly ObservedCall[]): void {
    for (const call of calls) {
      if (call.projectId !== null && call.projectId !== "") this.observedProject = call.projectId;
    }
  }

  // Gap routes: the transport never calls these. ---------------------------

  private async gapRequest(path: string, options: { method?: string; body?: unknown }): Promise<unknown> {
    const headers: Record<string, string> = { "X-GhostLab-Project": this.projectId() };
    const controller = new AbortController();
    const init: { method: string; headers: Record<string, string>; body?: string; signal: AbortSignal } = {
      method: options.method ?? "GET",
      headers,
      signal: controller.signal,
    };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await this.fetchImpl(`${this.baseUrl}${path}`, init);
      const text = await response.text();
      let data: unknown = null;
      try {
        data = text === "" ? null : (JSON.parse(text) as unknown);
      } catch {
        fail(toServiceFailure(new Error(`live service at ${this.baseUrl || "same-origin"} returned non-JSON for ${path}`), this.baseUrl));
      }
      if (!response.ok) {
        const detail = isRecord(data) && typeof data["detail"] === "string" ? (data["detail"] as string) : text;
        fail(toServiceFailure(new Error(`live service ${String(response.status)} for ${path}: ${detail}`), this.baseUrl));
      }
      return data;
    } catch (error) {
      if (error instanceof ServiceGuideError) throw error;
      if (error instanceof DOMException && error.name === "AbortError") {
        fail({ kind: "timeout", status: null, detail: `gap call ${path} timed out`, message: `Service call ${path} timed out; the config is preserved — retry.` });
      }
      fail(toServiceFailure(error, this.baseUrl));
    } finally {
      clearTimeout(timer);
    }
  }

  /** Run qualification (executes the env on purpose) and return the report. */
  async qualifyEnv(envId: string, manifestDigest: string | null): Promise<QualificationView> {
    const data = await this.gapRequest(`/v1/envs/${encodeURIComponent(envId)}/qualify`, {
      method: "POST",
      body: { manifest_digest: manifestDigest, fixture: null },
    });
    if (!isRecord(data) || !isRecord(data["report"])) {
      fail(toServiceFailure(new Error("live service returned a malformed qualify result"), this.baseUrl));
    }
    const report = (data as Record<string, unknown>)["report"] as Record<string, unknown>;
    const checks: unknown = report["checks"];
    if (!Array.isArray(checks)) {
      fail(toServiceFailure(new Error("live service returned malformed qualify checks"), this.baseUrl));
    }
    const digest: unknown = report["manifest_digest"];
    return {
      schemaVersion: 1,
      envId: needString(report, "env_id", "report"),
      envVersion: needString(report, "env_version", "report"),
      adapterRef: needString(report, "adapter_ref", "report"),
      codeDigest: needString(report, "code_digest", "report"),
      manifestDigest: digest === null ? null : typeof digest === "string" ? digest : String(digest),
      runnerVersion: needString(report, "runner_version", "report"),
      checks: (checks as unknown[]).map((entry) => {
        if (!isRecord(entry)) throw new Error("live service returned a malformed check");
        const passed: unknown = entry["passed"];
        if (typeof passed !== "boolean") throw new Error("live service returned a malformed check");
        return { name: needString(entry, "name", "check"), passed, detail: needString(entry, "detail", "check") };
      }),
      status: needString(report, "status", "report"),
      createdAt: needString(report, "created_at", "report"),
      label: "Live service: executed qualification; verdict from this report.",
    };
  }

  /** Standalone group read (reload recovery, status refresh). */
  async getGroup(groupId: string): Promise<RunGroupView> {
    const data = await this.gapRequest(`/v1/runs/${encodeURIComponent(groupId)}`, {});
    try {
      return parseRunGroup(data);
    } catch (error) {
      fail(toServiceFailure(error, this.baseUrl));
    }
  }

  /** Cancel every in-flight group observed during the current run. */
  async cancelRun(): Promise<string[]> {
    const targets = [...this.inflightGroups];
    const cancelled: string[] = [];
    for (const groupId of targets) {
      const data = await this.gapRequest(`/v1/runs/${encodeURIComponent(groupId)}/cancel`, { method: "POST", body: {} });
      if (isRecord(data) && data["cancelled"] === true) cancelled.push(groupId);
    }
    return cancelled;
  }

  /** Server-computed resolved-config diff. */
  async compareResolved(leftId: string, rightId: string): Promise<CompareResult> {
    const data = await this.gapRequest("/v1/compare", { method: "POST", body: { left_id: leftId, right_id: rightId } });
    try {
      return parseCompare(data);
    } catch (error) {
      fail(toServiceFailure(error, this.baseUrl));
    }
  }

  /** Actual recorded artifacts for one group (resolved, runs, echoes). */
  async fetchExport(groupId: string): Promise<ServerExportBundle> {
    const data = await this.gapRequest(`/v1/runs/${encodeURIComponent(groupId)}/export`, {});
    try {
      return parseExport(data);
    } catch (error) {
      fail(toServiceFailure(error, this.baseUrl));
    }
  }

  // Execution with reference capture + honest assembly. --------------------

  async runExperiment(config: EffectiveConfig): Promise<ExperimentEvidence> {
    prescanEffective(config);
    const recorder = new CallRecorder();
    this.running = true;
    this.inflightGroups = [];
    try {
      const evidence = await withRecording(recorder, () => this.live.runExperiment(config));
      this.noteObserved(recorder.calls);
      const assembled = this.assemble(recorder.calls, config, evidence);
      this.persistRun(assembled.evidence, config);
      return assembled.evidence;
    } catch (error) {
      this.noteObserved(recorder.calls);
      fail(toServiceFailure(error, this.baseUrl));
    } finally {
      this.running = false;
      this.inflightGroups = [];
    }
  }

  private assemble(
    calls: readonly ObservedCall[],
    config: EffectiveConfig,
    evidence: ExperimentEvidence,
  ): { evidence: ExperimentEvidence } {
    const { submits, starts, groups } = assembleReferences(calls);
    for (const start of starts) {
      if (!this.inflightGroups.includes(start.groupId)) this.inflightGroups.push(start.groupId);
    }
    const arms = buildArms(submits, starts, groups);
    if (arms.length === 0) {
      // Executed evidence without attributable references: present it with
      // the gap stated, never with invented ids.
      return {
        evidence: {
          ...evidence,
          notices: [
            ...evidence.notices,
            "Run references could not be captured from the service traffic: the totals and traces above are executed but unattributed. Re-run to capture references.",
          ],
        },
      };
    }
    const pins = submits.length > 0 && submits[0] !== undefined ? submits[0].pins : null;
    const mappingNotes = submits.length > 0 && submits[0] !== undefined ? submits[0].mappingNotes : [];
    const provenance = deriveProvenance(arms, pins);
    const references: ServiceReferences = {
      projectId: this.projectId(),
      baseUrl: this.baseUrl,
      arms,
      pins,
      mappingNotes,
    };
    const groupList = arms.map((arm) => `${arm.variant}=${shortId(arm.groupId)}`).join(", ");
    const submittedVariant = variantOf(config);
    const matched = arms.find((arm) => arm.variant === submittedVariant);
    const submittedNote =
      matched !== undefined
        ? `The submitted variant (${config.agent.adapter}, timeout_after_commit=${String(config.faults.timeoutAfterCommit)}) executed live as the ${matched.variant} branch (run ${shortId(matched.runId)}); companions executed live for comparison.`
        : `The submitted variant (${config.agent.adapter}, timeout_after_commit=${String(config.faults.timeoutAfterCommit)}) matches no canonical branch, so it executed live separately alongside the canonical branches.`;
    const envLine =
      pins === null
        ? "Executed against a registered env (pins unavailable)."
        : `Executed against registered env ${pins.envId} ${pins.envVersion} (code ${shortDigest(pins.codeDigest)}).`;
    return {
      evidence: {
        ...evidence,
        source: {
          experimentResult: `Executed by the service (${String(arms.length)} branch(es): ${groupList}); the diff below is computed in the browser from executed traces.`,
          capsule: "Assembled in the browser from executed faulted-branch evidence; not server-issued. Step 7 fetches the authoritative server export.",
          env: envLine,
        },
        notices: [
          `Live execution: ${String(arms.length)} branch(es) executed by the service; every total and trace comes from executed run bodies.`,
          submittedNote,
          provenance.modelCalls === 0
            ? "Verified: the controller recorded 0 model calls across all arms; no paid calls (this contract has no model/cost path)."
            : `Verified: the controller recorded ${String(provenance.modelCalls)} model call(s); provider unverified — treat as untrusted.`,
          "Diff, finding and capsule are assembled in the browser from executed evidence and labeled as such; a server-issued finding/capsule is not part of this contract.",
        ],
        references,
        provenance,
      },
    };
  }

  private persistRun(evidence: ExperimentEvidence, config: EffectiveConfig): void {
    const refs = evidence.references;
    if (refs === undefined) return;
    const stored: StoredRun = {
      version: 1,
      storedAt: new Date().toISOString(),
      submittedConfig: JSON.parse(JSON.stringify(config)) as EffectiveConfig,
      evidence: JSON.parse(JSON.stringify(evidence)) as ExperimentEvidence,
      arms: refs.arms.map((arm) => ({ variant: arm.variant, groupId: arm.groupId, resolvedId: arm.resolvedId, runDigest: arm.runDigest })),
    };
    saveStoredRun(this.storage, stored);
  }

  // Reload recovery: re-read live groups, verify, restore. ------------------

  async recoverStored(limit = STORAGE_CAP): Promise<RecoveryOutcome[]> {
    const stored = loadStoredRuns(this.storage).slice(-limit);
    const outcomes: RecoveryOutcome[] = [];
    for (const entry of stored) {
      const problems: string[] = [];
      let verified = true;
      for (const arm of entry.arms) {
        let group: RunGroupView;
        try {
          group = await this.getGroup(arm.groupId);
        } catch (error) {
          verified = false;
          const failure = toServiceFailure(error, this.baseUrl);
          problems.push(
            failure.kind === "not-found"
              ? `${arm.variant}: group handle expired (the service keeps groups in memory; a restart drops handles while stored runs survive in SQLite).`
              : `${arm.variant}: ${failure.message}`,
          );
          continue;
        }
        if (!TERMINAL_GROUP.includes(group.status)) {
          verified = false;
          problems.push(`${arm.variant}: group ${shortId(arm.groupId)} is ${group.status}, not terminal.`);
          continue;
        }
        const match = group.runs.find((run) => run.resolvedId === arm.resolvedId);
        if (match === undefined) {
          verified = false;
          problems.push(`${arm.variant}: no run for resolved ${shortId(arm.resolvedId)} in group ${shortId(arm.groupId)}.`);
        } else if (arm.runDigest !== "" && match.digest !== arm.runDigest) {
          verified = false;
          problems.push(`${arm.variant}: run digest changed since the stored run (stored ${shortDigest(arm.runDigest)}, live ${shortDigest(match.digest)}).`);
        }
      }
      const prior = entry.evidence.provenance;
      const evidence: ExperimentEvidence =
        prior === undefined
          ? { ...entry.evidence }
          : {
              ...entry.evidence,
              provenance: {
                ...prior,
                evidence: verified
                  ? "historical (stored assembly re-verified against live run records; digests match)"
                  : "historical (stored assembly; live verification failed — see problems)",
              },
            };
      outcomes.push({ stored: { ...entry, evidence }, verified, problems });
    }
    return outcomes;
  }
}

/** Type guard for the widened StepContext api union. */
export function isServiceGuide(api: { mode: EvidenceMode }): api is ServiceGuideApi {
  return api instanceof ServiceGuideApi;
}

