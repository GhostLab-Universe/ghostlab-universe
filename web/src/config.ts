/* Track D guided-run configuration: the single shape the UI and the API
 * client validate, submit and record. Pure module: zero imports, no DOM, no
 * JSX, erasable types only, so Node type-stripping can test it directly.
 *
 * Constraint sources (mirrored, not forked):
 * - fixture/scenario bounds: src/ghostlab/core.py (Fixture, Scenario)
 * - budget/objective bounds: schemas/v1/campaign_spec.schema.json,
 *   schemas/v1/budget_limits.schema.json
 * - proposal shape: schemas/v1/experiment_proposal.schema.json
 * - env/manifest rules: src/ghostlab/envs/manifest.py + registry.py patterns
 * The server re-validates with the same rules; the UI never invents bounds. */

export const GUIDED_CONFIG_SCHEMA_VERSION = 1;
export const WORLD_VERSION = "refund-world/1";
export const ADAPTER_VERSION = "refund-adapters/1";
export const GUIDED_STATE_VERSION = "track-d/1";

export type AgentKind = "vulnerable" | "repaired";
export type InvariantName =
  | "authorized_refunds"
  | "total_refund_within_payment"
  | "no_duplicate_full_refund"
  | "unique_idempotency_keys";

export const INVARIANT_NAMES: readonly InvariantName[] = [
  "authorized_refunds",
  "total_refund_within_payment",
  "no_duplicate_full_refund",
  "unique_idempotency_keys",
];

export interface EffectiveEnv {
  envId: string;
  envVersion: string;
  adapterRef: string;
  manifestDigest: string | null;
}

export interface EffectiveAgent {
  adapter: AgentKind;
  /** Operator note recorded with the run. Fixture-mode inference is scripted. */
  note: string;
}

export interface EffectiveFixture {
  orderId: string;
  customerId: string;
  paidMinor: number;
  currency: string;
}

export interface EffectiveFaults {
  timeoutAfterCommit: boolean;
}

export interface EffectiveChecks {
  invariants: InvariantName[];
}

export interface EffectiveLimits {
  maxToolCalls: number;
  maxModelCalls: number;
  maxInputTokens: number;
  maxOutputTokens: number;
  maxWallClockMs: number;
  maxParallelRuns: number;
  maxCostMinor: number | null;
  costCurrency: string | null;
}

export interface EffectiveConfig {
  schemaVersion: 1;
  env: EffectiveEnv;
  agent: EffectiveAgent;
  fixture: EffectiveFixture;
  faults: EffectiveFaults;
  checks: EffectiveChecks;
  limits: EffectiveLimits;
  objective: string;
  successCriteria: string[];
}

export interface ConfigError {
  path: string;
  message: string;
}

export type ConfigValidation =
  | { readonly ok: true; readonly config: EffectiveConfig }
  | { readonly ok: false; readonly errors: ConfigError[] };

/** Every editable UI control and the step that owns it. */
export interface ControlPath {
  path: string;
  step: number;
  label: string;
}

export const CONTROL_PATHS: readonly ControlPath[] = [
  { path: "env.envId", step: 1, label: "Environment" },
  { path: "env.manifestDigest", step: 1, label: "Manifest digest" },
  { path: "agent.adapter", step: 3, label: "Agent adapter" },
  { path: "agent.note", step: 3, label: "Agent note" },
  { path: "fixture.orderId", step: 4, label: "Order ID" },
  { path: "fixture.customerId", step: 4, label: "Customer ID" },
  { path: "fixture.paidMinor", step: 4, label: "Paid amount (minor units)" },
  { path: "fixture.currency", step: 4, label: "Currency" },
  { path: "faults.timeoutAfterCommit", step: 4, label: "Timeout after commit" },
  { path: "checks.invariants", step: 4, label: "Invariant focus" },
  { path: "limits.maxToolCalls", step: 4, label: "Max tool calls" },
  { path: "limits.maxModelCalls", step: 4, label: "Max model calls" },
  { path: "limits.maxInputTokens", step: 4, label: "Max input tokens" },
  { path: "limits.maxOutputTokens", step: 4, label: "Max output tokens" },
  { path: "limits.maxWallClockMs", step: 4, label: "Max wall clock (ms)" },
  { path: "limits.maxParallelRuns", step: 4, label: "Max parallel runs" },
  { path: "limits.maxCostMinor", step: 4, label: "Max cost (minor units)" },
  { path: "limits.costCurrency", step: 4, label: "Cost currency" },
  { path: "objective", step: 4, label: "Objective" },
  { path: "successCriteria", step: 4, label: "Success criteria" },
];

export function defaultConfig(): EffectiveConfig {
  return {
    schemaVersion: 1,
    env: {
      envId: "refund-env",
      envVersion: "refund-env/1",
      adapterRef: "ghostlab.envs.adapters:RefundEnvAdapter",
      manifestDigest: null,
    },
    agent: { adapter: "vulnerable", note: "" },
    fixture: {
      orderId: "synthetic-order-1",
      customerId: "synthetic-customer-1",
      paidMinor: 250000,
      currency: "INR",
    },
    faults: { timeoutAfterCommit: true },
    checks: { invariants: [...INVARIANT_NAMES] },
    limits: {
      maxToolCalls: 20,
      maxModelCalls: 20,
      maxInputTokens: 200000,
      maxOutputTokens: 20000,
      maxWallClockMs: 600000,
      maxParallelRuns: 2,
      maxCostMinor: null,
      costCurrency: null,
    },
    objective: "Find duplicate-refund failures after a payment timeout",
    successCriteria: ["A failing invariant is reproduced from a capsule"],
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isInt(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value);
}

function checkExactKeys(
  value: Record<string, unknown>,
  keys: readonly string[],
  path: string,
  errors: ConfigError[],
): boolean {
  let ok = true;
  for (const key of Object.keys(value)) {
    if (!keys.includes(key)) {
      errors.push({ path: path ? `${path}.${key}` : key, message: "unknown field" });
      ok = false;
    }
  }
  for (const key of keys) {
    if (!(key in value)) {
      errors.push({ path: path ? `${path}.${key}` : key, message: "missing field" });
      ok = false;
    }
  }
  return ok;
}

function checkId(value: unknown, maxLen: number): boolean {
  return typeof value === "string" && value.length >= 1 && value.length <= maxLen;
}

const ENV_ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,63}$/;
const ENV_VERSION_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._/+*-]{0,63}$/;
const DIGEST_PATTERN = /^[0-9a-f]{64}$/;
const CURRENCY_PATTERN = /^[A-Z]{3}$/;

export function validateEffectiveConfig(input: unknown): ConfigValidation {
  const errors: ConfigError[] = [];
  if (!isRecord(input)) return { ok: false, errors: [{ path: "", message: "config must be an object" }] };
  checkExactKeys(
    input,
    ["schemaVersion", "env", "agent", "fixture", "faults", "checks", "limits", "objective", "successCriteria"],
    "",
    errors,
  );
  if (input["schemaVersion"] !== 1) {
    errors.push({ path: "schemaVersion", message: "must be the integer 1" });
  }

  const env = input["env"];
  if (!isRecord(env)) {
    errors.push({ path: "env", message: "must be an object" });
  } else {
    checkExactKeys(env, ["envId", "envVersion", "adapterRef", "manifestDigest"], "env", errors);
    if (typeof env["envId"] !== "string" || !ENV_ID_PATTERN.test(env["envId"])) {
      errors.push({ path: "env.envId", message: "invalid env_id" });
    }
    if (typeof env["envVersion"] !== "string" || !ENV_VERSION_PATTERN.test(env["envVersion"])) {
      errors.push({ path: "env.envVersion", message: "invalid env_version" });
    }
    if (!checkId(env["adapterRef"], 200) || (env["adapterRef"] as string).includes(" ")) {
      errors.push({ path: "env.adapterRef", message: "must look like 'module:Class'" });
    } else if ((env["adapterRef"] as string).split(":").length !== 2) {
      errors.push({ path: "env.adapterRef", message: "must look like 'module:Class'" });
    }
    const digest = env["manifestDigest"];
    if (digest !== null && (typeof digest !== "string" || !DIGEST_PATTERN.test(digest))) {
      errors.push({ path: "env.manifestDigest", message: "must be null or a sha256 hex digest" });
    }
  }

  const agent = input["agent"];
  if (!isRecord(agent)) {
    errors.push({ path: "agent", message: "must be an object" });
  } else {
    checkExactKeys(agent, ["adapter", "note"], "agent", errors);
    if (agent["adapter"] !== "vulnerable" && agent["adapter"] !== "repaired") {
      errors.push({ path: "agent.adapter", message: "must be vulnerable or repaired" });
    }
    if (typeof agent["note"] !== "string" || agent["note"].length > 2000) {
      errors.push({ path: "agent.note", message: "must be a string of at most 2000 chars" });
    }
  }

  const fixture = input["fixture"];
  if (!isRecord(fixture)) {
    errors.push({ path: "fixture", message: "must be an object" });
  } else {
    checkExactKeys(fixture, ["orderId", "customerId", "paidMinor", "currency"], "fixture", errors);
    if (!checkId(fixture["orderId"], 100)) errors.push({ path: "fixture.orderId", message: "must be 1-100 chars" });
    if (!checkId(fixture["customerId"], 100)) {
      errors.push({ path: "fixture.customerId", message: "must be 1-100 chars" });
    }
    if (!checkId(fixture["currency"], 100)) errors.push({ path: "fixture.currency", message: "must be 1-100 chars" });
    const paid = fixture["paidMinor"];
    if (!isInt(paid) || paid < 1 || paid > 1000000000000) {
      errors.push({ path: "fixture.paidMinor", message: "must be an integer 1..1000000000000" });
    }
  }

  const faults = input["faults"];
  if (!isRecord(faults)) {
    errors.push({ path: "faults", message: "must be an object" });
  } else {
    checkExactKeys(faults, ["timeoutAfterCommit"], "faults", errors);
    if (typeof faults["timeoutAfterCommit"] !== "boolean") {
      errors.push({ path: "faults.timeoutAfterCommit", message: "must be a boolean" });
    }
  }

  const checks = input["checks"];
  if (!isRecord(checks)) {
    errors.push({ path: "checks", message: "must be an object" });
  } else {
    checkExactKeys(checks, ["invariants"], "checks", errors);
    const list = checks["invariants"];
    if (!Array.isArray(list) || list.length < 1 || list.length > 4) {
      errors.push({ path: "checks.invariants", message: "must list 1-4 invariants" });
    } else {
      const seen = new Set<string>();
      for (const name of list) {
        if (typeof name !== "string" || !INVARIANT_NAMES.includes(name as InvariantName) || seen.has(name)) {
          errors.push({ path: "checks.invariants", message: `invalid or duplicate invariant '${String(name)}'` });
          break;
        }
        seen.add(name);
      }
    }
  }

  const limits = input["limits"];
  if (!isRecord(limits)) {
    errors.push({ path: "limits", message: "must be an object" });
  } else {
    checkExactKeys(
      limits,
      ["maxToolCalls", "maxModelCalls", "maxInputTokens", "maxOutputTokens", "maxWallClockMs", "maxParallelRuns", "maxCostMinor", "costCurrency"],
      "limits",
      errors,
    );
    // maxToolCalls satisfies both the scenario bound (1..1000) and the
    // budget bound (0..1e12); the stricter range is enforced here.
    const toolCalls = limits["maxToolCalls"];
    if (!isInt(toolCalls) || toolCalls < 1 || toolCalls > 1000) {
      errors.push({ path: "limits.maxToolCalls", message: "must be an integer 1..1000" });
    }
    const intBounds: ReadonlyArray<readonly [string, number, number]> = [
      ["maxModelCalls", 0, 1000000000000],
      ["maxInputTokens", 0, 1000000000000],
      ["maxOutputTokens", 0, 1000000000000],
      ["maxWallClockMs", 1, 1000000000000],
      ["maxParallelRuns", 1, 64],
    ];
    for (const [key, low, high] of intBounds) {
      const value: unknown = limits[key];
      if (!isInt(value) || value < low || value > high) {
        errors.push({ path: `limits.${key}`, message: `must be an integer ${low}..${high}` });
      }
    }
    const cost = limits["maxCostMinor"];
    const currency = limits["costCurrency"];
    if (cost !== null && (!isInt(cost) || cost < 0 || cost > 1000000000000000)) {
      errors.push({ path: "limits.maxCostMinor", message: "must be null or an integer 0..1000000000000000" });
    }
    if (currency !== null && (typeof currency !== "string" || !CURRENCY_PATTERN.test(currency))) {
      errors.push({ path: "limits.costCurrency", message: "must be null or a 3-letter code" });
    }
    if ((cost === null) !== (currency === null)) {
      errors.push({ path: "limits.maxCostMinor", message: "cost amount and currency are set together or both null" });
    }
  }

  if (typeof input["objective"] !== "string" || input["objective"].length < 1 || input["objective"].length > 2000) {
    errors.push({ path: "objective", message: "must be 1-2000 chars" });
  }
  const criteria = input["successCriteria"];
  if (!Array.isArray(criteria) || criteria.length < 1 || criteria.length > 16) {
    errors.push({ path: "successCriteria", message: "must list 1-16 criteria" });
  } else {
    for (const item of criteria) {
      if (typeof item !== "string" || item.length < 1 || item.length > 200) {
        errors.push({ path: "successCriteria", message: "each criterion must be 1-200 chars" });
        break;
      }
    }
  }

  if (errors.length > 0) return { ok: false, errors };
  return { ok: true, config: input as unknown as EffectiveConfig };
}

/** Read a dotted path (array indices allowed) for review rendering. */
export function getPath(config: EffectiveConfig, path: string): unknown {
  let current: unknown = config;
  for (const part of path.split(".")) {
    if (Array.isArray(current)) {
      const index = Number(part);
      if (!Number.isInteger(index)) return undefined;
      current = current[index];
    } else if (isRecord(current)) {
      current = current[part];
    } else {
      return undefined;
    }
  }
  return current;
}

/** Flatten to [path, value] rows in CONTROL_PATHS order for review/evidence. */
export function flattenConfig(config: EffectiveConfig): Array<readonly [string, unknown]> {
  return CONTROL_PATHS.map((control) => [control.path, getPath(config, control.path)] as const);
}

/** Immutable update of one dotted path; arrays addressable by index. */
export function setPath(config: EffectiveConfig, path: string, value: unknown): EffectiveConfig {
  const clone: EffectiveConfig = JSON.parse(JSON.stringify(config)) as EffectiveConfig;
  const parts = path.split(".");
  let current: unknown = clone;
  for (let index = 0; index < parts.length - 1; index += 1) {
    const part = parts[index] as string;
    if (Array.isArray(current)) {
      current = current[Number(part)];
    } else if (isRecord(current)) {
      current = current[part];
    } else {
      throw new Error(`cannot set '${path}'`);
    }
  }
  const last = parts[parts.length - 1] as string;
  if (Array.isArray(current)) {
    current[Number(last)] = value;
  } else if (isRecord(current)) {
    current[last] = value;
  } else {
    throw new Error(`cannot set '${path}'`);
  }
  return clone;
}

export interface CampaignSpecPayload {
  schema_version: 1;
  objective: string;
  success_criteria: string[];
  world_version: "refund-world/1";
  budget: {
    max_model_calls: number;
    max_tool_calls: number;
    max_input_tokens: number;
    max_output_tokens: number;
    max_cost_minor: number | null;
    cost_currency: string | null;
    max_wall_clock_ms: number;
    max_parallel_runs: number;
  };
}

/** Map the effective config onto the frozen campaign_spec/1 shape. */
export function toCampaignSpec(config: EffectiveConfig): CampaignSpecPayload {
  return {
    schema_version: 1,
    objective: config.objective,
    success_criteria: [...config.successCriteria],
    world_version: WORLD_VERSION,
    budget: {
      max_model_calls: config.limits.maxModelCalls,
      max_tool_calls: config.limits.maxToolCalls,
      max_input_tokens: config.limits.maxInputTokens,
      max_output_tokens: config.limits.maxOutputTokens,
      max_cost_minor: config.limits.maxCostMinor,
      cost_currency: config.limits.costCurrency,
      max_wall_clock_ms: config.limits.maxWallClockMs,
      max_parallel_runs: config.limits.maxParallelRuns,
    },
  };
}

export interface ExperimentProposalPayload {
  schema_version: 1;
  hypothesis: string;
  adapter: AgentKind;
  scenario: { timeout_after_commit: boolean; max_tool_calls: number };
  expected_signal: string;
}

/**
 * Map the effective config onto the frozen experiment_proposal/1 shape.
 * Hypothesis/signal text is derived deterministically from the config so the
 * proposal carries no free-form model output at this stage.
 */
export function toExperimentProposal(config: EffectiveConfig): ExperimentProposalPayload {
  const fault = config.faults.timeoutAfterCommit ? "with a post-commit timeout" : "without a fault";
  return {
    schema_version: 1,
    hypothesis:
      `A ${config.agent.adapter} agent run over ${config.fixture.orderId} ${fault} ` +
      `exercises the recorded refund behavior.`,
    adapter: config.agent.adapter,
    scenario: {
      timeout_after_commit: config.faults.timeoutAfterCommit,
      max_tool_calls: config.limits.maxToolCalls,
    },
    expected_signal: `Invariant focus: ${config.checks.invariants.join(", ")}; ledger totals per executed evidence.`,
  };
}

/* Manifest v1 shape validation (mirrors ghostlab/envs/manifest.py rules). */

export interface ManifestTool {
  name: string;
  parameters: string[];
}

export interface ManifestV1 {
  manifest_version: 1;
  env_id: string;
  env_version: string;
  adapter_ref: string;
  display_name: string;
  tools: ManifestTool[];
  reproduction_modes: string[];
  fault_hooks: string[];
  notes: string;
}

export type ManifestValidation =
  | { readonly ok: true; readonly manifest: ManifestV1 }
  | { readonly ok: false; readonly errors: ConfigError[] };

const TOOL_NAME_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const PARAM_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const MODE_PATTERN = /^[a-z][a-z0-9-]{0,63}$/;
// Hook names use underscores (e.g. timeout_after_commit), unlike modes.
const HOOK_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const URL_PATTERN = /[a-zA-Z][a-zA-Z0-9+.-]*:\/\//;
const WINDOWS_ABS_PATTERN = /^[A-Za-z]:[\\/]|^\\\\/;

function manifestScan(node: unknown, where: string, errors: ConfigError[]): void {
  if (typeof node === "string") {
    if (URL_PATTERN.test(node)) errors.push({ path: where, message: "must not contain a URL" });
    else if (node.startsWith("/") || WINDOWS_ABS_PATTERN.test(node)) {
      errors.push({ path: where, message: "must not contain an absolute path" });
    } else if (node.includes("\0")) errors.push({ path: where, message: "must not contain a NUL byte" });
  } else if (Array.isArray(node)) {
    node.forEach((item, index) => manifestScan(item, `${where}[${index}]`, errors));
  } else if (isRecord(node)) {
    for (const key of Object.keys(node)) manifestScan(node[key], `${where}.${key}`, errors);
  }
}

export function validateManifestShape(input: unknown): ManifestValidation {
  const errors: ConfigError[] = [];
  if (!isRecord(input)) return { ok: false, errors: [{ path: "", message: "manifest must be an object" }] };
  checkExactKeys(
    input,
    ["manifest_version", "env_id", "env_version", "adapter_ref", "display_name", "tools", "reproduction_modes", "fault_hooks", "notes"],
    "",
    errors,
  );
  if (input["manifest_version"] !== 1) errors.push({ path: "manifest_version", message: "unsupported manifest_version" });
  if (typeof input["env_id"] !== "string" || !ENV_ID_PATTERN.test(input["env_id"])) {
    errors.push({ path: "env_id", message: "invalid manifest env_id" });
  }
  if (typeof input["env_version"] !== "string" || !ENV_VERSION_PATTERN.test(input["env_version"])) {
    errors.push({ path: "env_version", message: "invalid manifest env_version" });
  }
  if (!checkId(input["adapter_ref"], 200)) errors.push({ path: "adapter_ref", message: "invalid manifest adapter_ref" });
  if (!checkId(input["display_name"], 200)) errors.push({ path: "display_name", message: "invalid manifest display_name" });
  const tools = input["tools"];
  if (!Array.isArray(tools) || tools.length < 1 || tools.length > 32) {
    errors.push({ path: "tools", message: "must list 1-32 tools" });
  } else {
    const seen = new Set<string>();
    for (const entry of tools) {
      if (!isRecord(entry) || Object.keys(entry).length !== 2 || typeof entry["name"] !== "string" || !Array.isArray(entry["parameters"])) {
        errors.push({ path: "tools", message: "tool entries need exactly name and parameters" });
        break;
      }
      const name = entry["name"];
      const params = entry["parameters"] as unknown[];
      if (!TOOL_NAME_PATTERN.test(name) || seen.has(name)) {
        errors.push({ path: "tools", message: `invalid or duplicate tool '${name}'` });
        break;
      }
      seen.add(name);
      if (
        params.length > 16 ||
        !(params as unknown[]).every((p) => typeof p === "string" && PARAM_PATTERN.test(p)) ||
        new Set(params as unknown[]).size !== (params as unknown[]).length
      ) {
        errors.push({ path: "tools", message: `invalid parameters for tool '${name}'` });
        break;
      }
    }
  }
  const modes = input["reproduction_modes"];
  if (!Array.isArray(modes) || modes.length < 1 || modes.length > 8) {
    errors.push({ path: "reproduction_modes", message: "invalid length" });
  } else if (!modes.every((m) => typeof m === "string" && MODE_PATTERN.test(m)) || new Set(modes).size !== modes.length) {
    errors.push({ path: "reproduction_modes", message: "invalid or duplicate entry" });
  }
  const hooks = input["fault_hooks"];
  if (!Array.isArray(hooks) || hooks.length > 16) {
    errors.push({ path: "fault_hooks", message: "invalid length" });
  } else if (!hooks.every((m) => typeof m === "string" && HOOK_PATTERN.test(m)) || new Set(hooks).size !== hooks.length) {
    errors.push({ path: "fault_hooks", message: "invalid or duplicate entry" });
  }
  if (typeof input["notes"] !== "string" || input["notes"].length > 2000) {
    errors.push({ path: "notes", message: "invalid manifest notes" });
  }
  manifestScan(input, "manifest", errors);
  if (errors.length > 0) return { ok: false, errors };
  return { ok: true, manifest: input as unknown as ManifestV1 };
}

/**
 * Canonical JSON: sorted keys, (",", ":") separators, ensure_ascii escaping,
 * non-finite numbers rejected. Matches Track A canonical_digest input.
 */
export function canonicalize(value: unknown): string {
  const sorted = (node: unknown): unknown => {
    if (Array.isArray(node)) return node.map(sorted);
    if (isRecord(node)) {
      const out: Record<string, unknown> = {};
      for (const key of Object.keys(node).sort()) out[key] = sorted(node[key]);
      return out;
    }
    return node;
  };
  const text = JSON.stringify(sorted(value), (_key, item: unknown) => {
    if (typeof item === "number" && !Number.isFinite(item)) throw new Error("non-finite number");
    return item;
  });
  // ensure_ascii: escape every non-ASCII code unit as lowercase \uXXXX.
  return text.replace(/[\u0080-\uffff]/g, (ch) => `\\u${ch.charCodeAt(0).toString(16).padStart(4, "0")}`);
}
