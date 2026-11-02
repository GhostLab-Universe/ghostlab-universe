import type { Failure, DataSource, Result, RunName } from "../data/source.ts";
import { SERVICE_ORIGIN } from "../data/source.ts";
import type { ErrorInfo } from "./api.generated.ts";
import { OPERATIONS, type OperationId } from "./operations.generated.ts";

/**
 * M07 client. Paths, methods and required response fields all come from the generated
 * operation table, so a contract change fails the build rather than a guess at runtime.
 *
 * The browser sends no credentials of its own: the session is the server's. Requests
 * stay same-origin so a session cookie can apply, and no project, principal, role or
 * scope is ever sent, because none of those is a contract field.
 */
export interface ClientOptions {
  /** Same-origin by default. Tests pass a stub. */
  fetch?: typeof globalThis.fetch;
  baseUrl?: string;
}

interface CallInput {
  path?: Record<string, string>;
  query?: Record<string, number | string>;
  body?: unknown;
  idempotencyKey?: string;
}

const PROXY_STATUSES = new Set([502, 503, 504]);

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function serviceFailure(status: number, body: unknown): Failure | null {
  if (!isObject(body) || !isObject(body.error)) return null;
  const { code, message } = body.error;
  if (typeof code !== "string" || typeof message !== "string") return null;
  const reason = typeof body.reason === "string" ? body.reason : null;
  return { kind: "service", status, error: body.error as unknown as ErrorInfo, reason };
}

export function createApiClient(options: ClientOptions = {}): DataSource {
  const send = options.fetch ?? globalThis.fetch.bind(globalThis);
  const baseUrl = options.baseUrl ?? "";

  async function request(id: OperationId, input: CallInput): Promise<Response | Failure> {
    const operation = OPERATIONS[id];
    const path = operation.path.replace(/\{(\w+)\}/g, (_, name: string) => {
      const value = input.path?.[name];
      if (value === undefined) throw new Error(`${id} needs path parameter ${name}`);
      return encodeURIComponent(value);
    });
    const query = new URLSearchParams();
    for (const [name, value] of Object.entries(input.query ?? {})) {
      query.set(name, String(value));
    }
    const headers: Record<string, string> = { Accept: "application/json" };
    if (input.body !== undefined) headers["Content-Type"] = "application/json";
    if (operation.idempotencyKey) {
      if (input.idempotencyKey === undefined) throw new Error(`${id} needs an Idempotency-Key`);
      headers["Idempotency-Key"] = input.idempotencyKey;
    }
    const url = `${baseUrl}${path}${query.size > 0 ? `?${query}` : ""}`;
    try {
      return await send(url, {
        method: operation.method,
        headers,
        credentials: "same-origin",
        ...(input.body === undefined ? {} : { body: JSON.stringify(input.body) }),
      });
    } catch (error) {
      return { kind: "transport", message: error instanceof Error ? error.message : "Request failed" };
    }
  }

  async function failureFrom(id: OperationId, response: Response): Promise<Failure> {
    const text = await response.text().catch(() => "");
    let body: unknown = null;
    try {
      body = JSON.parse(text);
    } catch {
      // Handled below: a body that isn't ErrorResponse is not a service verdict.
    }
    const failure = serviceFailure(response.status, body);
    if (failure !== null) return failure;
    if (PROXY_STATUSES.has(response.status)) {
      return { kind: "transport", message: `The service is unreachable (HTTP ${response.status})` };
    }
    return { kind: "invalid_response", operation: id, message: `HTTP ${response.status} without an error body` };
  }

  async function json<T>(id: OperationId, input: CallInput): Promise<Result<T>> {
    const response = await request(id, input);
    if (!(response instanceof Response)) return { ok: false, failure: response };
    if (response.status !== OPERATIONS[id].success) {
      return { ok: false, failure: await failureFrom(id, response) };
    }
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      return { ok: false, failure: { kind: "invalid_response", operation: id, message: "Response is not JSON" } };
    }
    const required = OPERATIONS[id].required as readonly string[];
    const missing = isObject(body) ? required.filter((key) => !(key in body)) : required;
    if (!isObject(body) || missing.length > 0) {
      const message = `Response is missing ${missing.join(", ") || "its body"}`;
      return { ok: false, failure: { kind: "invalid_response", operation: id, message } };
    }
    return { ok: true, data: body as T, origins: SERVICE_ORIGIN };
  }

  const run = (campaign_id: string, experiment_id: string, name: RunName) => ({
    path: { campaign_id, experiment_id, run: name },
  });

  return {
    kind: "api",
    getSession: () => json("getSession", {}),
    createCampaign: (spec, idempotencyKey, scripted_scenario) =>
      json("createCampaign", {
        body: spec,
        idempotencyKey,
        ...(scripted_scenario === undefined ? {} : { query: { scripted_scenario } }),
      }),
    getCampaign: (campaign_id) => json("getCampaign", { path: { campaign_id } }),
    listEvents: (campaign_id, after) => json("listCampaignEvents", { path: { campaign_id }, query: { after } }),
    cancelCampaign: (campaign_id) => json("cancelCampaign", { path: { campaign_id } }),
    resumeCampaign: (campaign_id) => json("resumeCampaign", { path: { campaign_id } }),
    getExperiment: (campaign_id, experiment_id) => json("getExperiment", { path: { campaign_id, experiment_id } }),
    getRun: (campaign_id, experiment_id, name) => json("getRun", run(campaign_id, experiment_id, name)),
    exportCapsule: (campaign_id, experiment_id, name) => json("exportCapsule", run(campaign_id, experiment_id, name)),
    replayCapsule: (campaign_id, experiment_id, name) =>
      json("replayCampaignCapsule", run(campaign_id, experiment_id, name)),
    getApproval: (approval_id) => json("getApproval", { path: { approval_id } }),
    listExtEnvironments: () => json("listExtEnvironments", {}),
    getExtEnvironment: (world) => json("getExtEnvironment", { path: { world } }),
    getExtFixture: (world, name) => json("getExtFixture", { path: { world, name } }),
    qualifyExtension: (request) => json("qualifyExtension", { body: request }),
    assessExtFidelity: (request) => json("assessExtFidelity", { body: request }),
    runExtCampaign: (request) => json("runExtCampaign", { body: request }),
    inspectExtCapsule: (request) => json("inspectExtCapsule", { body: request }),
    replayExtRecorded: (request) => json("replayExtRecorded", { body: request }),
    retestExtFresh: (request) => json("retestExtFresh", { body: request }),
    minimizeExtFaults: (request) => json("minimizeExtFaults", { body: request }),
    createExpConfig: (request) => json("createExpConfig", { body: request }),
    listExpConfigs: () => json("listExpConfigs", {}),
    getExpConfig: (config_id) => json("getExpConfig", { path: { config_id } }),
    resolveExpConfig: (config_id, request) => json("resolveExpConfig", { path: { config_id }, body: request }),
    listExpResolved: () => json("listExpResolved", {}),
    getExpResolved: (resolved_id) => json("getExpResolved", { path: { resolved_id } }),
    runExpResolved: (resolved_id, request) => json("runExpResolved", { path: { resolved_id }, body: request }),
    listExpRuns: (resolved_id) => json("listExpRuns", resolved_id === undefined ? {} : { query: { resolved_id } }),
    getExpRun: (run_id) => json("getExpRun", { path: { run_id } }),
    diffExpResolved: (left_id, right_id) => json("diffExpResolved", { query: { left_id, right_id } }),
    getExpPolicy: () => json("getExpPolicy", {}),
    decideApproval: (submission, idempotencyKey) =>
      json("decideApproval", { path: { approval_id: submission.approval_id }, body: submission, idempotencyKey }),
    async downloadArtifact(artifact_id) {
      const response = await request("downloadArtifact", { path: { artifact_id } });
      if (!(response instanceof Response)) return { ok: false, failure: response };
      if (response.status !== OPERATIONS.downloadArtifact.success) {
        return { ok: false, failure: await failureFrom("downloadArtifact", response) };
      }
      return { ok: true, data: await response.blob(), origins: SERVICE_ORIGIN };
    },
  };
}
