/* B5 read-only backend status surface.
 *
 * Pure renderer over fetched JSON plus unit-testable formatters. No fixture
 * data: every row renders service JSON, or an explicit loading/error state.
 * Transport config (base URL, project) comes from the existing client
 * instance (ServiceGuideApi); the small GET helper below covers only the
 * routes the frozen clients do not expose (backend list/status plus a raw
 * group read for job state) and never re-implements their flows. */

import { createElement, useEffect, useState, type ReactNode } from "react";
import type { ExperimentApi } from "./api";
import type { ServiceGuideApi } from "./serviceClient";

export const DEFAULT_BACKEND_NAME = "trusted-local";

// Local element helpers mirror web/src/ui.ts markup (same tags and
// classNames) without a runtime import: under node type-stripping,
// extensionless relative imports do not resolve, and the app tsconfig
// forbids .ts import extensions, so this module keeps runtime imports
// to the react package (repo convention for node-tested modules).
type Props = Record<string, unknown>;
type Child = ReactNode;

function h(tag: string, props?: Props | null, ...children: Child[]): ReactNode {
  return createElement(tag, props ?? null, ...children);
}

function tag(name: string): (props?: Props | null, ...children: Child[]) => ReactNode {
  return (props, ...children) => h(name, props ?? null, ...children);
}

const div = tag("div");
const p = tag("p");
const h2 = tag("h2");
const ul = tag("ul");
const li = tag("li");
const code = tag("code");
const strong = tag("strong");
const table = tag("table");
const tbody = tag("tbody");
const tr = tag("tr");
const th = tag("th");
const td = tag("td");

function banner(kind: "info" | "warn" | "error" | "ok", title: string, body?: Child): ReactNode {
  return div(
    { className: `banner banner-${kind}`, role: kind === "error" ? "alert" : "status" },
    strong(null, title),
    body === undefined ? null : p(null, body),
  );
}

function kvTable(rows: ReadonlyArray<readonly [string, Child]>): ReactNode {
  return table(
    { className: "kv" },
    tbody(
      null,
      ...rows.map(([key, value], index) => tr({ key: `${index}-${key}` }, th(null, key), td(null, value))),
    ),
  );
}

/**
 * Structural service-client check, behavior-equivalent to isServiceGuide
 * for real clients (service adapter vs fixture/transport). instanceof
 * would need a runtime serviceClient import, which node type-stripping
 * cannot resolve, so the panel detects the live adapter structurally.
 */
export function isLiveServiceClient(api: ExperimentApi | ServiceGuideApi): api is ServiceGuideApi {
  if (api.mode !== "live" || !("serviceBaseUrl" in api)) return false;
  return typeof api.projectId === "function";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function needString(node: Record<string, unknown>, key: string, where: string): string {
  const value: unknown = node[key];
  if (typeof value !== "string") throw new Error(`service returned malformed ${where}.${key}`);
  return value;
}

function needBool(node: Record<string, unknown>, key: string, where: string): boolean {
  const value: unknown = node[key];
  if (typeof value !== "boolean") throw new Error(`service returned malformed ${where}.${key}`);
  return value;
}

function needRecord(node: Record<string, unknown>, key: string, where: string): Record<string, unknown> {
  const value: unknown = node[key];
  if (!isRecord(value)) throw new Error(`service returned malformed ${where}.${key}`);
  return value;
}

function optString(node: Record<string, unknown>, key: string, where: string): string | null {
  const value: unknown = node[key];
  if (value === null || value === undefined) return null;
  if (typeof value !== "string") throw new Error(`service returned malformed ${where}.${key}`);
  return value;
}

function optBool(node: Record<string, unknown>, key: string, where: string): boolean | null {
  const value: unknown = node[key];
  if (value === null || value === undefined) return null;
  if (typeof value !== "boolean") throw new Error(`service returned malformed ${where}.${key}`);
  return value;
}

function needStringList(node: Record<string, unknown>, key: string, where: string): string[] {
  const value: unknown = node[key];
  if (!Array.isArray(value) || !value.every((entry) => typeof entry === "string")) {
    throw new Error(`service returned malformed ${where}.${key}`);
  }
  return [...(value as string[])];
}

function optStringList(node: Record<string, unknown>, key: string, where: string): string[] {
  const value: unknown = node[key];
  if (value === null || value === undefined) return [];
  if (!Array.isArray(value) || !value.every((entry) => typeof entry === "string")) {
    throw new Error(`service returned malformed ${where}.${key}`);
  }
  return [...(value as string[])];
}

export interface BackendCapabilitiesView {
  executeRuns: boolean;
  cancel: boolean;
  lifecycle: string[];
  network: string;
  modelAccess: string;
}

export interface BackendQualificationCheckView {
  name: string;
  passed: boolean;
  detail: string;
}

export interface BackendQualificationView {
  status: string;
  qualifiedForIsolation: boolean;
  detail: string;
  checks: BackendQualificationCheckView[];
}

export interface BackendLimitsView {
  enforced: boolean;
  cpu: string | null;
  memory: string | null;
  pids: string | null;
  wallClock: string | null;
  output: string | null;
  detail: string;
}

export interface BackendPackageView {
  kind: string;
  image: string | null;
  detail: string;
}

export interface BackendDescriptorView {
  name: string;
  version: string;
  trustedLocal: boolean;
  isolation: string;
  isolationScope: string;
  limitations: string[];
  available: boolean;
  unavailableReason: string | null;
  capabilities: BackendCapabilitiesView;
  qualification: BackendQualificationView;
  limits: BackendLimitsView;
  packageIdentity: BackendPackageView;
}

export interface BackendLivenessView {
  backend: string;
  reachable: boolean;
  checkedAt: string;
  detail: string;
}

export interface BackendStatusView {
  projectId: string;
  backend: BackendDescriptorView;
  liveness: BackendLivenessView;
}

export interface BackendListView {
  projectId: string;
  backends: BackendDescriptorView[];
}

/** 503-shaped refusal body. Rendered as a failure state, never retried. */
export interface BackendFailureView {
  status: number;
  error: string;
  detail: string;
}

/** Job/termination/evidence state derived from a raw group body. */
export interface BackendJobView {
  groupId: string;
  status: string;
  backendName: string | null;
  jobId: string | null;
  jobState: string | null;
  terminationReason: string | null;
  terminationDetail: string | null;
  runsRecorded: number;
  runsWithEvidence: number;
  /** Present on export/compare bodies; null when the body says nothing. */
  truncated: boolean | null;
  /** Present on export/compare bodies; empty when the body says nothing. */
  missing: string[];
}

function parseQualification(node: unknown): BackendQualificationView {
  if (!isRecord(node)) throw new Error("service returned malformed backend.qualification");
  const checks: unknown = node["checks"];
  if (!Array.isArray(checks)) throw new Error("service returned malformed backend.qualification.checks");
  return {
    status: needString(node, "status", "backend.qualification"),
    qualifiedForIsolation: needBool(node, "qualified_for_isolation", "backend.qualification"),
    detail: needString(node, "detail", "backend.qualification"),
    checks: checks.map((entry): BackendQualificationCheckView => {
      if (!isRecord(entry)) throw new Error("service returned a malformed qualification check");
      return {
        name: needString(entry, "name", "check"),
        passed: needBool(entry, "passed", "check"),
        detail: needString(entry, "detail", "check"),
      };
    }),
  };
}

export function parseBackendDescriptor(node: unknown): BackendDescriptorView {
  if (!isRecord(node)) throw new Error("service returned a malformed backend descriptor");
  const capabilities = needRecord(node, "capabilities", "backend");
  const limits = needRecord(node, "limits", "backend");
  const identity = needRecord(node, "package_identity", "backend");
  const unavailable: unknown = node["unavailable_reason"];
  return {
    name: needString(node, "name", "backend"),
    version: needString(node, "version", "backend"),
    trustedLocal: needBool(node, "trusted_local", "backend"),
    isolation: needString(node, "isolation", "backend"),
    isolationScope: needString(node, "isolation_scope", "backend"),
    limitations: needStringList(node, "limitations", "backend"),
    available: needBool(node, "available", "backend"),
    unavailableReason: unavailable === null ? null : typeof unavailable === "string" ? unavailable : String(unavailable),
    capabilities: {
      executeRuns: needBool(capabilities, "execute_runs", "backend.capabilities"),
      cancel: needBool(capabilities, "cancel", "backend.capabilities"),
      lifecycle: needStringList(capabilities, "lifecycle", "backend.capabilities"),
      network: needString(capabilities, "network", "backend.capabilities"),
      modelAccess: needString(capabilities, "model_access", "backend.capabilities"),
    },
    qualification: parseQualification(node["qualification"]),
    limits: {
      enforced: needBool(limits, "enforced", "backend.limits"),
      cpu: optString(limits, "cpu", "backend.limits"),
      memory: optString(limits, "memory", "backend.limits"),
      pids: optString(limits, "pids", "backend.limits"),
      wallClock: optString(limits, "wall_clock", "backend.limits"),
      output: optString(limits, "output", "backend.limits"),
      detail: needString(limits, "detail", "backend.limits"),
    },
    packageIdentity: {
      kind: needString(identity, "kind", "backend.package_identity"),
      image: optString(identity, "image", "backend.package_identity"),
      detail: needString(identity, "detail", "backend.package_identity"),
    },
  };
}

export function parseBackendList(node: unknown): BackendListView {
  if (!isRecord(node)) throw new Error("service returned a malformed backend list");
  const backends: unknown = node["backends"];
  if (!Array.isArray(backends)) throw new Error("service returned malformed backends");
  return {
    projectId: needString(node, "project_id", "backends"),
    backends: backends.map(parseBackendDescriptor),
  };
}

export function parseBackendStatus(node: unknown): BackendStatusView {
  if (!isRecord(node)) throw new Error("service returned a malformed backend status");
  const liveness = needRecord(node, "live_liveness", "backend.status");
  return {
    projectId: needString(node, "project_id", "backend.status"),
    backend: parseBackendDescriptor(node["backend"]),
    liveness: {
      backend: needString(liveness, "backend", "live_liveness"),
      reachable: needBool(liveness, "reachable", "live_liveness"),
      checkedAt: needString(liveness, "checked_at", "live_liveness"),
      detail: needString(liveness, "detail", "live_liveness"),
    },
  };
}

/** Null unless the body is an {error, detail} refusal. Never throws. */
export function parseBackendFailure(httpStatus: number, body: unknown): BackendFailureView | null {
  if (!isRecord(body)) return null;
  const error: unknown = body["error"];
  const detail: unknown = body["detail"];
  if (typeof error !== "string" || typeof detail !== "string") return null;
  return { status: httpStatus, error, detail };
}

const TERMINAL_JOB = ["completed", "cancelled", "failed"];

export function parseGroupJob(node: unknown): BackendJobView {
  if (!isRecord(node)) throw new Error("service returned a malformed run group");
  const status = needString(node, "status", "group");
  const backend: unknown = node["backend"];
  const job: unknown = node["job"];
  const termination: unknown = node["termination"];
  const backendName = isRecord(backend) ? optString(backend, "name", "group.backend") : null;
  const jobId = isRecord(job) ? optString(job, "job_id", "group.job") : null;
  const jobState = isRecord(job) ? optString(job, "state", "group.job") : null;
  let terminationReason: string | null = null;
  let terminationDetail: string | null = null;
  if (termination !== null && termination !== undefined) {
    if (!isRecord(termination)) throw new Error("service returned malformed group.termination");
    terminationReason = optString(termination, "reason", "group.termination");
    terminationDetail = optString(termination, "detail", "group.termination");
  }
  if (terminationReason === null && TERMINAL_JOB.includes(status)) terminationReason = status;
  const runs: unknown = node["runs"];
  if (!Array.isArray(runs)) throw new Error("service returned malformed group.runs");
  let runsWithEvidence = 0;
  for (const record of runs) {
    if (isRecord(record) && isRecord(record["run"]) && record["run"]["evidence"] !== null && record["run"]["evidence"] !== undefined) {
      runsWithEvidence += 1;
    }
  }
  return {
    groupId: needString(node, "group_id", "group"),
    status,
    backendName,
    jobId,
    jobState,
    terminationReason,
    terminationDetail,
    runsRecorded: runs.length,
    runsWithEvidence,
    truncated: optBool(node, "truncated", "group"),
    missing: optStringList(node, "missing", "group"),
  };
}

// Pure formatters (unit-tested, no React). -------------------------------

export function formatBackendHeadline(descriptor: BackendDescriptorView): string {
  const label = descriptor.trustedLocal ? "trusted-local execution (explicit; not a sandbox)" : descriptor.isolation;
  if (descriptor.available) return `${descriptor.name} (${descriptor.version}): ${label} — available`;
  const reason = descriptor.unavailableReason ?? "unavailable";
  return `${descriptor.name} (${descriptor.version}): ${label} — UNAVAILABLE: ${reason}`;
}

export function formatQualification(descriptor: BackendDescriptorView): string {
  const flag = descriptor.qualification.qualifiedForIsolation ? "yes" : "no";
  return `${descriptor.qualification.status}; qualified_for_isolation=${flag}; ${descriptor.qualification.detail}`;
}

export function formatIsolation(descriptor: BackendDescriptorView): string {
  return `${descriptor.isolation} — scope: ${descriptor.isolationScope}`;
}

export function formatLimitations(descriptor: BackendDescriptorView): string[] {
  return [...descriptor.limitations];
}

export function formatPackageIdentity(descriptor: BackendDescriptorView): string {
  const image = descriptor.packageIdentity.image ?? "none";
  return `${descriptor.packageIdentity.kind} (image: ${image}); ${descriptor.packageIdentity.detail}`;
}

export function formatLimits(descriptor: BackendDescriptorView): string[] {
  const limits = descriptor.limits;
  const line = (label: string, value: string | null): string =>
    `${label}: ${value ?? "not set"}`;
  return [
    `enforced: ${limits.enforced ? "yes" : "no"}; ${limits.detail}`,
    line("cpu", limits.cpu),
    line("memory", limits.memory),
    line("pids", limits.pids),
    line("wall_clock", limits.wallClock),
    line("output", limits.output),
    `network: ${descriptor.capabilities.network}`,
    `model access: ${descriptor.capabilities.modelAccess}`,
  ];
}

export function formatLiveness(liveness: BackendLivenessView): string {
  const flag = liveness.reachable ? "reachable" : "UNREACHABLE";
  return `${liveness.backend}: ${flag} at ${liveness.checkedAt}; ${liveness.detail}`;
}

export function formatJobState(group: BackendJobView): string {
  const job = group.jobId ?? "unassigned";
  const state = group.jobState ?? group.status;
  const backend = group.backendName ?? "unstated";
  return `job ${job}: ${state} (group ${group.groupId} ${group.status}; backend ${backend})`;
}

export function formatTermination(group: BackendJobView): string {
  if (group.terminationReason === null) return `not terminated (group ${group.status})`;
  const detail = group.terminationDetail === null || group.terminationDetail === "" ? "no detail" : group.terminationDetail;
  return `${group.terminationReason}: ${detail}`;
}

export function formatEvidenceCompleteness(group: BackendJobView): string {
  const runs = `${String(group.runsRecorded)} run(s) recorded, ${String(group.runsWithEvidence)} with evidence`;
  const truncated = group.truncated === null ? "truncation not stated" : group.truncated ? "TRUNCATED: output cut short" : "not truncated";
  const missing = group.missing.length === 0 ? "nothing reported missing" : `missing: ${group.missing.join(", ")}`;
  return `${runs}; ${truncated}; ${missing}`;
}

export function formatKnownBackends(backends: readonly BackendDescriptorView[]): string {
  if (backends.length === 0) return "none reported";
  return backends.map((entry) => `${entry.name} (${entry.available ? "available" : "unavailable"})`).join(", ");
}

// Panel model: pure renderer input (unit-tested, no React). --------------

export type BackendPanelModel =
  | { kind: "loading" }
  | { kind: "unavailable"; message: string }
  | { kind: "failure"; backendName: string; failure: BackendFailureView }
  | { kind: "error"; message: string }
  | { kind: "ready"; status: BackendStatusView; backends: BackendDescriptorView[]; group: BackendJobView | null };

/** Assemble a ready model from fetched JSON. Throws on malformed bodies. */
export function buildReadyModel(statusNode: unknown, listNode: unknown, groupNode: unknown | null): BackendPanelModel {
  const status = parseBackendStatus(statusNode);
  const list = parseBackendList(listNode);
  const group = groupNode === null ? null : parseGroupJob(groupNode);
  return { kind: "ready", status, backends: list.backends, group };
}

/**
 * Assemble a failure model from a refusal response. Accepts the 503
 * backend_unavailable shape; anything else throws so the caller renders
 * a generic error instead of mislabeling it.
 */
export function buildFailureModel(httpStatus: number, body: unknown, backendName: string): BackendPanelModel {
  const failure = parseBackendFailure(httpStatus, body);
  if (httpStatus !== 503 || failure === null) {
    throw new Error(`service refused backend '${backendName}' with unexpected status ${String(httpStatus)}`);
  }
  return { kind: "failure", backendName, failure };
}

/** Pure renderer over the panel model. No fetching, no hooks. */
export function renderBackendStatus(model: BackendPanelModel): ReactNode {
  if (model.kind === "loading") return p({ "data-testid": "backend-status-loading" }, "Loading backend status…");
  if (model.kind === "unavailable") {
    return div(
      { "data-testid": "backend-status-panel" },
      h2(null, "Execution backend status"),
      banner("info", "Backend status unavailable", model.message),
    );
  }
  if (model.kind === "error") {
    return div(
      { "data-testid": "backend-status-panel" },
      h2(null, "Execution backend status"),
      banner("error", "Backend status failed", model.message),
    );
  }
  if (model.kind === "failure") {
    return div(
      { "data-testid": "backend-status-panel" },
      h2(null, "Execution backend status"),
      banner("error", `Backend '${model.backendName}' unavailable`, `${model.failure.error} (HTTP ${String(model.failure.status)}): ${model.failure.detail}`),
      p({ "data-testid": "backend-failure" }, `Refused without fallback. ${model.failure.detail}`),
    );
  }
  const descriptor = model.status.backend;
  const rows: ReadonlyArray<readonly [string, string]> = [
    ["Selected backend", formatBackendHeadline(descriptor)],
    ["Qualification", formatQualification(descriptor)],
    ["Isolation", formatIsolation(descriptor)],
    ["Package / image", formatPackageIdentity(descriptor)],
    ["Liveness (live probe)", formatLiveness(model.status.liveness)],
    ["Job state", model.group === null ? "no run selected" : formatJobState(model.group)],
    ["Termination", model.group === null ? "no run selected" : formatTermination(model.group)],
    ["Evidence", model.group === null ? "no run selected" : formatEvidenceCompleteness(model.group)],
    ["Known backends", formatKnownBackends(model.backends)],
  ];
  return div(
    { "data-testid": "backend-status-panel" },
    h2(null, "Execution backend status"),
    descriptor.available
      ? null
      : banner("error", `Backend '${descriptor.name}' unavailable`, descriptor.unavailableReason ?? "unavailable"),
    kvTable(rows.map(([key, value]): readonly [string, string] => [key, value] as const)),
    h2(null, "Isolation limitations"),
    ul(null, ...formatLimitations(descriptor).map((line, index) => li({ key: index }, line))),
    h2(null, "Effective resource / network limits"),
    ul(null, ...formatLimits(descriptor).map((line, index) => li({ key: index }, code(null, line)))),
  );
}

// Minimal transport for the three routes the frozen clients lack. --------

export interface BackendServiceRef {
  baseUrl: string;
  projectId: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

export interface ServiceGetResult {
  status: number;
  body: unknown;
}

export async function serviceGet(ref: BackendServiceRef, path: string): Promise<ServiceGetResult> {
  const fetchImpl = ref.fetchImpl ?? globalThis.fetch;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ref.timeoutMs ?? 10000);
  try {
    const response = await fetchImpl(`${ref.baseUrl}${path}`, {
      method: "GET",
      headers: { "X-GhostLab-Project": ref.projectId },
      signal: controller.signal,
    });
    const text = await response.text();
    let body: unknown = null;
    try {
      body = text === "" ? null : (JSON.parse(text) as unknown);
    } catch {
      throw new Error(`service returned non-JSON for ${path}`);
    }
    return { status: response.status, body };
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error(`service call ${path} timed out; retry`);
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export async function fetchBackendList(ref: BackendServiceRef): Promise<BackendListView> {
  const { status, body } = await serviceGet(ref, "/v1/backends");
  if (status !== 200) {
    const failure = parseBackendFailure(status, body);
    throw new Error(failure === null ? `service status ${String(status)} for /v1/backends` : `service ${String(status)} for /v1/backends: ${failure.detail}`);
  }
  return parseBackendList(body);
}

export interface BackendStatusFetch {
  httpStatus: number;
  status: BackendStatusView | null;
  failure: BackendFailureView | null;
}

export async function fetchBackendStatus(ref: BackendServiceRef, name: string): Promise<BackendStatusFetch> {
  const path = `/v1/backends/${encodeURIComponent(name)}/status`;
  const { status, body } = await serviceGet(ref, path);
  if (status === 200) return { httpStatus: status, status: parseBackendStatus(body), failure: null };
  const failure = parseBackendFailure(status, body);
  if (status === 503 && failure !== null) return { httpStatus: status, status: null, failure };
  throw new Error(failure === null ? `service status ${String(status)} for ${path}` : `service ${String(status)} for ${path}: ${failure.detail}`);
}

export async function fetchGroupJob(ref: BackendServiceRef, groupId: string): Promise<BackendJobView> {
  const path = `/v1/runs/${encodeURIComponent(groupId)}`;
  const { status, body } = await serviceGet(ref, path);
  if (status !== 200) {
    const failure = parseBackendFailure(status, body);
    throw new Error(failure === null ? `service status ${String(status)} for ${path}` : `service ${String(status)} for ${path}: ${failure.detail}`);
  }
  return parseGroupJob(body);
}

// Component: mounts on the run/results view via App wiring. ---------------

export interface BackendStatusPanelProps {
  api: ExperimentApi | ServiceGuideApi;
  groupId: string | null;
  backendName?: string;
}

export function BackendStatusPanel(props: BackendStatusPanelProps): ReactNode {
  const backendName = props.backendName ?? DEFAULT_BACKEND_NAME;
  const [model, setModel] = useState<BackendPanelModel>({ kind: "loading" });
  useEffect(() => {
    const api = props.api;
    const groupId = props.groupId;
    if (!isLiveServiceClient(api)) {
      setModel({
        kind: "unavailable",
        message: "Backend status needs the live service; this session is not on the service path, so no backend state is shown.",
      });
      return;
    }
    const ref: BackendServiceRef = { baseUrl: api.serviceBaseUrl, projectId: api.projectId() };
    let cancelled = false;
    setModel({ kind: "loading" });
    (async (): Promise<void> => {
      try {
        const list = await fetchBackendList(ref);
        const fetched = await fetchBackendStatus(ref, backendName);
        if (fetched.failure !== null) {
          if (!cancelled) setModel({ kind: "failure", backendName, failure: fetched.failure });
          return;
        }
        if (fetched.status === null) throw new Error("service returned an empty backend status");
        const group = groupId === null ? null : await fetchGroupJob(ref, groupId);
        if (!cancelled) setModel({ kind: "ready", status: fetched.status, backends: list.backends, group });
      } catch (error) {
        if (!cancelled) setModel({ kind: "error", message: error instanceof Error ? error.message : String(error) });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [props.api, props.groupId, backendName]);
  return renderBackendStatus(model);
}
