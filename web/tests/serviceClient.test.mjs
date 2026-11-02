// Unit tests for the service-facing guide adapter (web/src/serviceClient.ts).
// Runs on stock Node via type stripping; the service is a stubbed fetch, the
// transport a fake with the LiveExperimentApi shape. No network, no fixtures.
//   node --experimental-strip-types --test web/tests/serviceClient.test.mjs

import assert from "node:assert/strict";
import { describe, it, afterEach } from "node:test";
import { defaultConfig } from "../src/config.ts";
import {
  FALLBACK_PROJECT_ID,
  STORAGE_CAP,
  STORAGE_KEY,
  CallRecorder,
  ServiceGuideApi,
  ServiceGuideError,
  assembleReferences,
  buildArms,
  deriveProvenance,
  isServiceGuide,
  loadStoredRuns,
  clearStoredRuns,
  parseCompare,
  parseExport,
  parseRunGroup,
  prescanEffective,
  saveStoredRun,
  toServiceFailure,
} from "../src/serviceClient.ts";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

function memStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, v),
    removeItem: (k) => map.delete(k),
    _map: map,
  };
}

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Stub fetch: routes are [method, path, handler] in order; records calls. */
function stubFetch(routes) {
  const calls = [];
  const impl = async (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url;
    calls.push({ url, method: init.method ?? "GET", headers: init.headers ?? {}, body: init.body });
    for (const [method, path, handler] of routes) {
      if ((init.method ?? "GET") === method && url.endsWith(path)) return handler(url, init);
    }
    return jsonResponse(404, { error: "not_found", detail: `no route ${url}` });
  };
  impl.calls = calls;
  return impl;
}

const PINS = {
  env_id: "refund-env",
  env_version: "refund-env/1",
  adapter_ref: "ghostlab.envs.adapters:RefundEnvAdapter",
  code_digest: "ab".repeat(32),
  manifest_digest: null,
  world_version: "refund-world/1",
  adapter_version: "refund-adapters/1",
  controller_version: "experiment-controller/1",
  discovered_tools: [],
  discovered_modes: [],
};

const NOTES = [
  { effective_path: "env.envId", requested_path: "env_id", action: "mapped", detail: "exact" },
];

function submitBody(variant, n) {
  return {
    project_id: "browser-guide",
    requested_id: `req-${variant}`,
    resolved_id: `res-${variant}`,
    resolved: { pins: PINS, effective: {} },
    mapping_notes: NOTES,
  };
}

function runRecord(runId, resolvedId, digest, modelCalls = 0) {
  return {
    run_id: runId,
    project_id: "browser-guide",
    resolved_id: resolvedId,
    repetition: 0,
    attempt: 0,
    digest,
    created_at: "2026-11-03T00:00:00Z",
    run: {
      status: "completed",
      verdict: resolvedId === "res-faulted" ? "fail" : "pass",
      model_calls: modelCalls,
      tool_calls: 4,
      wall_clock_ms: 12,
      seed: 0,
      error: null,
      evidence: { snapshot: { order: {}, ledger: [], total_refunded_minor: 1 }, trace: [] },
      checks: {},
    },
  };
}

function groupBody(groupId, resolvedId, runId, digest, modelCalls = 0) {
  return {
    project_id: "browser-guide",
    group_id: groupId,
    resolved_id: resolvedId,
    status: "completed",
    runs: [runRecord(runId, resolvedId, digest, modelCalls)],
    error: null,
    created_at: "2026-11-03T00:00:00Z",
    updated_at: "2026-11-03T00:00:01Z",
  };
}

function variantConfig(base, adapter, timeout) {
  const copy = JSON.parse(JSON.stringify(base));
  copy.agent.adapter = adapter;
  copy.faults.timeoutAfterCommit = timeout;
  return copy;
}

/** Fake transport: performs scripted service traffic via global fetch. */
function fakeLiveTraffic({ modelCalls = 0 } = {}) {
  const seen = [];
  return {
    mode: "live",
    seen,
    listEnvs: async () => {
      seen.push("listEnvs");
      const res = await globalThis.fetch("/v1/envs", { headers: { "X-GhostLab-Project": "browser-guide" } });
      const data = await res.json();
      return data.envs;
    },
    getCapabilities: async () => ({ envId: "x" }),
    getQualification: async () => ({ envId: "x" }),
    importManifest: async () => ({ manifestDigest: "d" }),
    runExperiment: async (config) => {
      seen.push("runExperiment");
      const variants = [
        ["baseline", variantConfig(config, "vulnerable", false)],
        ["faulted", variantConfig(config, "vulnerable", true)],
        ["repaired", variantConfig(config, "repaired", true)],
      ];
      for (const [name, effective] of variants) {
        const sub = await globalThis.fetch("/v1/guide/submit", {
          method: "POST",
          headers: { "X-GhostLab-Project": "browser-guide", "Content-Type": "application/json" },
          body: JSON.stringify({ effective_config: effective }),
        });
        const submitted = await sub.json();
        const start = await globalThis.fetch("/v1/runs", {
          method: "POST",
          headers: { "X-GhostLab-Project": "browser-guide", "Content-Type": "application/json" },
          body: JSON.stringify({ resolved_id: submitted.resolved_id }),
        });
        const started = await start.json();
        await globalThis.fetch(`/v1/runs/${started.group_id}`, {
          headers: { "X-GhostLab-Project": "browser-guide" },
        });
        void name;
      }
      return {
        mode: "live",
        submittedAt: "2026-11-03T00:00:02Z",
        effectiveConfig: config,
        source: { experimentResult: "transport", capsule: "transport", env: "transport" },
        result: { baseline: {}, faulted: {}, repaired: {}, diff: {} },
        capsule: { payload: {}, sha256: "0".repeat(64) },
        finding: { finding_id: "f" },
        notices: ["transport notice"],
      };
    },
  };
}

describe("prescanEffective", () => {
  it("accepts the honest defaults", () => {
    prescanEffective(defaultConfig());
  });

  it("refuses secret-like text with a path and no echo", () => {
    const config = defaultConfig();
    config.agent.note = "key is MODEL_API_KEY=abc123";
    assert.throws(() => prescanEffective(config), /agent\.note.*API_KEY/);
    try {
      prescanEffective(config);
      assert.fail("must throw");
    } catch (error) {
      assert.ok(!String(error.message).includes("abc123"), "value must not echo");
    }
  });
});

describe("toServiceFailure", () => {
  it("maps network failure to connection with preserved-config wording", () => {
    const failure = toServiceFailure(new TypeError("fetch failed"), "");
    assert.equal(failure.kind, "connection");
    assert.match(failure.message, /preserved/);
    assert.match(failure.message, /never.*fixtures|no fixtures/i);
  });

  it("maps transport status lines to typed kinds", () => {
    assert.equal(toServiceFailure(new Error("live service 400 for /v1/guide/submit: bad"), "").kind, "config");
    assert.equal(toServiceFailure(new Error("live service 403 for /v1/runs/g: x"), "").kind, "forbidden");
    assert.equal(toServiceFailure(new Error("live service 404 for /v1/runs/g: x"), "").kind, "not-found");
    assert.equal(toServiceFailure(new Error("live service 409 for /v1/runs: spent"), "").kind, "budget");
    assert.equal(toServiceFailure(new Error("live service 413 for /v1/x: big"), "").kind, "too-large");
    assert.equal(toServiceFailure(new Error("live service 500 for /v1/x: boom"), "").kind, "internal");
  });

  it("maps timeout and cancel distinctly", () => {
    assert.equal(toServiceFailure(new Error("live run timed out waiting for completion"), "").kind, "timeout");
    assert.equal(toServiceFailure(new Error("live run was cancelled"), "").kind, "cancelled");
  });

  it("passes ServiceGuideError through", () => {
    const original = new ServiceGuideError({ kind: "budget", status: 409, detail: "spent", message: "spent" });
    assert.equal(toServiceFailure(original, "").kind, "budget");
  });
});

describe("CallRecorder", () => {
  it("records traffic without changing responses", async () => {
    const inner = stubFetch([["GET", "/v1/envs", async () => jsonResponse(200, { envs: [1] })]]);
    const recorder = new CallRecorder();
    globalThis.fetch = recorder.wrap(inner);
    const res = await globalThis.fetch("/v1/envs", { headers: { "X-GhostLab-Project": "p1" } });
    assert.deepEqual(await res.json(), { envs: [1] });
    assert.equal(recorder.calls.length, 1);
    assert.equal(recorder.calls[0].method, "GET");
    assert.equal(recorder.calls[0].status, 200);
    assert.equal(recorder.calls[0].projectId, "p1");
    assert.deepEqual(recorder.calls[0].responseBody, { envs: [1] });
  });
});

describe("ServiceGuideApi delegation", () => {
  it("delegates listEnvs and observes the project", async () => {
    globalThis.fetch = stubFetch([
      ["GET", "/v1/envs", async () => jsonResponse(200, { envs: [{ env_id: "e1" }] })],
    ]);
    const api = new ServiceGuideApi({ live: fakeLiveTraffic(), storage: null });
    assert.equal(api.projectObserved(), false);
    assert.equal(api.projectId(), FALLBACK_PROJECT_ID);
    const envs = await api.listEnvs();
    assert.deepEqual(envs, [{ env_id: "e1" }]);
    assert.equal(api.projectObserved(), true);
    assert.equal(api.projectId(), "browser-guide");
    assert.equal(api.mode, "live");
    assert.equal(isServiceGuide(api), true);
  });

  it("never falls back to fixtures: transport failure rejects typed", async () => {
    globalThis.fetch = async () => {
      throw new TypeError("fetch failed");
    };
    const api = new ServiceGuideApi({ live: fakeLiveTraffic(), storage: null });
    await assert.rejects(() => api.listEnvs(), (error) => {
      assert.ok(error instanceof ServiceGuideError);
      assert.equal(error.kind, "connection");
      assert.match(error.message, /no fixtures were substituted/);
      assert.equal(error.result, undefined);
      assert.equal(error.evidence, undefined);
      return true;
    });
  });

  it("maps a 409 from the transport to budget", async () => {
    const live = fakeLiveTraffic();
    live.listEnvs = async () => {
      throw new Error("live service 409 for /v1/runs: budget envelope exhausted");
    };
    const api = new ServiceGuideApi({ live, storage: null });
    await assert.rejects(() => api.listEnvs(), (error) => error instanceof ServiceGuideError && error.kind === "budget");
  });
});

describe("gap routes", () => {
  it("qualifyEnv posts manifest digest and maps the report", async () => {
    const fetchImpl = stubFetch([
      [
        "POST",
        "/v1/envs/refund-env/qualify",
        async (url, init) => {
          const body = JSON.parse(init.body);
          assert.deepEqual(body, { manifest_digest: "d123", fixture: null });
          assert.equal(init.headers["X-GhostLab-Project"], "browser-guide");
          assert.equal(init.headers["Content-Type"], "application/json");
          return jsonResponse(200, {
            project_id: "browser-guide",
            report: {
              env_id: "refund-env",
              env_version: "refund-env/1",
              adapter_ref: "m:C",
              code_digest: "c",
              manifest_digest: "d123",
              runner_version: "q/1",
              checks: [{ name: "reset", passed: true, detail: "ok" }],
              status: "qualified-for-declared",
              created_at: "t",
            },
          });
        },
      ],
    ]);
    const api = new ServiceGuideApi({ live: fakeLiveTraffic(), fetchImpl, storage: null });
    const report = await api.qualifyEnv("refund-env", "d123");
    assert.equal(report.status, "qualified-for-declared");
    assert.equal(report.checks.length, 1);
    assert.equal(fetchImpl.calls.length, 1);
  });

  it("compareResolved posts both ids and parses diffs", async () => {
    const fetchImpl = stubFetch([
      [
        "POST",
        "/v1/compare",
        async (url, init) => {
          assert.deepEqual(JSON.parse(init.body), { left_id: "res-a", right_id: "res-b" });
          return jsonResponse(200, {
            project_id: "p",
            left_id: "res-a",
            right_id: "res-b",
            left_digest: "la",
            right_digest: "lb",
            equal: false,
            diff_count: 1,
            truncated: false,
            diffs: [{ path: "effective.faults", left: [], right: [1] }],
          });
        },
      ],
    ]);
    const api = new ServiceGuideApi({ live: fakeLiveTraffic(), fetchImpl, storage: null });
    const result = await api.compareResolved("res-a", "res-b");
    assert.equal(result.equal, false);
    assert.equal(result.diffs[0].path, "effective.faults");
  });

  it("fetchExport parses the recorded bundle", async () => {
    const fetchImpl = stubFetch([
      [
        "GET",
        "/v1/runs/grp-1/export",
        async () =>
          jsonResponse(200, {
            project_id: "p",
            group_id: "grp-1",
            resolved_id: "res-faulted",
            requested_id: "req-faulted",
            status: "completed",
            effective_config: defaultConfig(),
            resolved: { pins: PINS },
            runs: [runRecord("run-1", "res-faulted", "d1")],
            mapping_notes: NOTES,
            digests: { effective_digest: "e", resolved_digest: "r", run_digests: ["d1"] },
            exported_at: "t",
          }),
      ],
    ]);
    const api = new ServiceGuideApi({ live: fakeLiveTraffic(), fetchImpl, storage: null });
    const bundle = await api.fetchExport("grp-1");
    assert.equal(bundle.runs.length, 1);
    assert.equal(bundle.runs[0].verdict, "fail");
    assert.equal(bundle.mappingNotes.length, 1);
    assert.equal(bundle.effectiveConfig.fixture.paidMinor, 250000);
  });

  it("invokes fetch bound (Chrome illegal-invocation regression)", async () => {
    async function strictFetch(url) {
      assert.equal(this, globalThis, "fetch must be called bound to globalThis");
      assert.ok(String(url).endsWith("/v1/runs/grp-1"));
      return jsonResponse(200, groupBody("grp-1", "res-faulted", "run-1", "d1"));
    }
    const api = new ServiceGuideApi({ live: fakeLiveTraffic(), fetchImpl: strictFetch, storage: null });
    const group = await api.getGroup("grp-1");
    assert.equal(group.groupId, "grp-1");
  });

  it("cancelRun with no in-flight groups calls nothing", async () => {
    const fetchImpl = stubFetch([]);
    const api = new ServiceGuideApi({ live: fakeLiveTraffic(), fetchImpl, storage: null });
    assert.deepEqual(await api.cancelRun(), []);
    assert.equal(fetchImpl.calls.length, 0);
  });

  it("gap 404 surfaces as not-found with the service detail", async () => {
    const fetchImpl = stubFetch([
      ["GET", "/v1/runs/nope", async () => jsonResponse(404, { error: "not_found", detail: "unknown run group 'nope'" })],
    ]);
    const api = new ServiceGuideApi({ live: fakeLiveTraffic(), fetchImpl, storage: null });
    await assert.rejects(() => api.getGroup("nope"), (error) => {
      assert.ok(error instanceof ServiceGuideError);
      assert.equal(error.kind, "not-found");
      assert.match(error.message, /unknown run group/);
      return true;
    });
  });
});

describe("runExperiment assembly", () => {
  function trafficRoutes({ modelCalls = 0 } = {}) {
    return [
      [
        "POST",
        "/v1/guide/submit",
        async (url, init) => {
          const effective = JSON.parse(init.body).effective_config;
          const adapter = effective.agent.adapter;
          const timeout = effective.faults.timeoutAfterCommit;
          const variant = adapter === "repaired" ? "repaired" : timeout ? "faulted" : "baseline";
          return jsonResponse(200, submitBody(variant));
        },
      ],
      [
        "POST",
        "/v1/runs",
        async (url, init) => {
          const resolved = JSON.parse(init.body).resolved_id;
          const variant = resolved.replace("res-", "");
          return jsonResponse(200, {
            project_id: "browser-guide",
            group_id: `grp-${variant}`,
            resolved_id: resolved,
            status: "running",
          });
        },
      ],
      [
        "GET",
        "/v1/runs/grp-baseline",
        async () => jsonResponse(200, groupBody("grp-baseline", "res-baseline", "run-baseline", "dig-b", modelCalls)),
      ],
      [
        "GET",
        "/v1/runs/grp-faulted",
        async () => jsonResponse(200, groupBody("grp-faulted", "res-faulted", "run-faulted", "dig-f", modelCalls)),
      ],
      [
        "GET",
        "/v1/runs/grp-repaired",
        async () => jsonResponse(200, groupBody("grp-repaired", "res-repaired", "run-repaired", "dig-r", modelCalls)),
      ],
    ];
  }

  it("attaches references, pins, notes and verified provenance", async () => {
    globalThis.fetch = stubFetch(trafficRoutes());
    const storage = memStorage();
    const api = new ServiceGuideApi({ live: fakeLiveTraffic(), storage });
    const evidence = await api.runExperiment(defaultConfig());
    assert.equal(evidence.mode, "live");
    assert.ok(evidence.references, "references attached");
    assert.deepEqual(
      evidence.references.arms.map((a) => a.variant),
      ["baseline", "faulted", "repaired"],
    );
    assert.equal(evidence.references.arms[1].runId, "run-faulted");
    assert.equal(evidence.references.arms[1].verdict, "fail");
    assert.equal(evidence.references.arms[1].runDigest, "dig-f");
    assert.equal(evidence.references.pins.worldVersion, "refund-world/1");
    assert.equal(evidence.references.mappingNotes.length, 1);
    assert.equal(evidence.provenance.modelCalls, 0);
    assert.match(evidence.provenance.target, /deterministic-adapter/);
    assert.match(evidence.provenance.evidence, /executed/);
    assert.match(evidence.provenance.spend, /no paid calls/);
    assert.match(evidence.source.capsule, /not server-issued/);
    assert.ok(!evidence.notices.some((n) => n === "transport notice"), "transport notices replaced by verified ones");
    const stored = loadStoredRuns(storage);
    assert.equal(stored.length, 1);
    assert.equal(stored[0].arms.length, 3);
    assert.equal(stored[0].submittedConfig.fixture.paidMinor, 250000);
  });

  it("derives model-backed provenance from recorded model calls", async () => {
    globalThis.fetch = stubFetch(trafficRoutes({ modelCalls: 2 }));
    const api = new ServiceGuideApi({ live: fakeLiveTraffic(), storage: null });
    const evidence = await api.runExperiment(defaultConfig());
    assert.equal(evidence.provenance.modelCalls, 6);
    assert.match(evidence.provenance.target, /model-backed/);
    assert.match(evidence.provenance.target, /unverified/);
  });

  it("states the gap when no references were observable", async () => {
    const live = fakeLiveTraffic();
    live.runExperiment = async (config) => ({
      mode: "live",
      submittedAt: "t",
      effectiveConfig: config,
      source: { experimentResult: "s", capsule: "s", env: "s" },
      result: {},
      capsule: {},
      finding: {},
      notices: ["done"],
    });
    globalThis.fetch = stubFetch([]);
    const api = new ServiceGuideApi({ live, storage: null });
    const evidence = await api.runExperiment(defaultConfig());
    assert.equal(evidence.references, undefined);
    assert.ok(evidence.notices.some((n) => /could not be captured/.test(n)));
  });

  it("refuses secret-like configs before any traffic", async () => {
    const fetchImpl = stubFetch([]);
    globalThis.fetch = fetchImpl;
    const api = new ServiceGuideApi({ live: fakeLiveTraffic(), storage: null });
    const config = defaultConfig();
    config.objective = "uses BEARER token123";
    await assert.rejects(() => api.runExperiment(config), /secret-like/);
    assert.equal(fetchImpl.calls.length, 0);
  });
});

describe("deriveProvenance", () => {
  it("labels zero-call arms deterministic with counts", () => {
    const arms = [
      { variant: "baseline", modelCalls: 0 },
      { variant: "faulted", modelCalls: 0 },
    ];
    const provenance = deriveProvenance(arms, null);
    assert.equal(provenance.modelCalls, 0);
    assert.match(provenance.target, /0 model calls across 2/);
    assert.match(provenance.explorer, /no explorer/);
  });
});

describe("assembleReferences/buildArms", () => {
  it("skips malformed mid-poll bodies and keeps the terminal one", () => {
    const good = groupBody("grp-faulted", "res-faulted", "run-f", "dig-f");
    const calls = [
      {
        method: "POST",
        path: "/v1/guide/submit",
        status: 200,
        projectId: "browser-guide",
        requestBody: { effective_config: variantConfig(defaultConfig(), "vulnerable", true) },
        responseBody: submitBody("faulted"),
      },
      {
        method: "POST",
        path: "/v1/runs",
        status: 200,
        projectId: "browser-guide",
        requestBody: { resolved_id: "res-faulted" },
        responseBody: { resolved_id: "res-faulted", group_id: "grp-faulted" },
      },
      { method: "GET", path: "/v1/runs/grp-faulted", status: 200, projectId: "p", requestBody: null, responseBody: { nope: 1 } },
      { method: "GET", path: "/v1/runs/grp-faulted", status: 200, projectId: "p", requestBody: null, responseBody: good },
    ];
    const { submits, starts, groups } = assembleReferences(calls);
    assert.equal(submits.length, 1);
    assert.equal(starts.length, 1);
    assert.equal(groups.length, 1);
    const arms = buildArms(submits, starts, groups);
    assert.equal(arms.length, 1);
    assert.equal(arms[0].variant, "faulted");
    assert.equal(arms[0].runDigest, "dig-f");
  });

  it("ignores error responses", () => {
    const calls = [
      { method: "GET", path: "/v1/runs/g", status: 404, projectId: "p", requestBody: null, responseBody: { error: "not_found" } },
    ];
    const { submits, starts, groups } = assembleReferences(calls);
    assert.deepEqual([submits.length, starts.length, groups.length], [0, 0, 0]);
  });
});

describe("parsers", () => {
  it("parseRunGroup names the malformed field", () => {
    assert.throws(() => parseRunGroup({}), /malformed run group/);
    assert.throws(() => parseRunGroup({ project_id: "p" }), /malformed run group/);
  });

  it("parseCompare requires boolean flags", () => {
    assert.throws(
      () =>
        parseCompare({ left_id: "a", right_id: "b", left_digest: "x", right_digest: "y", equal: "yes", diff_count: 0, truncated: false, diffs: [] }),
      /malformed compare/,
    );
  });

  it("parseExport requires run digests", () => {
    assert.throws(() => parseExport({ project_id: "p" }), /malformed export/);
  });
});

describe("stored runs", () => {
  function storedRun() {
    return {
      version: 1,
      storedAt: "t",
      submittedConfig: defaultConfig(),
      evidence: {
        mode: "live",
        provenance: {
          environment: "e",
          target: "t",
          explorer: "x",
          evidence: "executed",
          modelCalls: 0,
          spend: "s",
        },
      },
      arms: [{ variant: "faulted", groupId: "grp-faulted", resolvedId: "res-faulted", runDigest: "dig-f" }],
    };
  }

  it("saves, loads, caps and clears", () => {
    const storage = memStorage();
    assert.deepEqual(loadStoredRuns(storage), []);
    saveStoredRun(storage, storedRun());
    assert.equal(loadStoredRuns(storage).length, 1);
    for (let i = 0; i < STORAGE_CAP + 3; i++) saveStoredRun(storage, storedRun());
    assert.equal(loadStoredRuns(storage).length, STORAGE_CAP);
    clearStoredRuns(storage);
    assert.deepEqual(loadStoredRuns(storage), []);
    assert.equal(storage._map.has(STORAGE_KEY), false);
  });

  it("tolerates corrupt storage", () => {
    const storage = memStorage();
    storage.setItem(STORAGE_KEY, "{oops");
    assert.deepEqual(loadStoredRuns(storage), []);
    storage.setItem(STORAGE_KEY, JSON.stringify([{ version: 999 }]));
    assert.deepEqual(loadStoredRuns(storage), []);
  });

  it("recoverStored verifies digests live", async () => {
    const storage = memStorage();
    saveStoredRun(storage, storedRun());
    const fetchImpl = stubFetch([
      ["GET", "/v1/runs/grp-faulted", async () => jsonResponse(200, groupBody("grp-faulted", "res-faulted", "run-f", "dig-f"))],
    ]);
    const api = new ServiceGuideApi({ live: fakeLiveTraffic(), fetchImpl, storage });
    const outcomes = await api.recoverStored();
    assert.equal(outcomes.length, 1);
    assert.equal(outcomes[0].verified, true);
    assert.match(outcomes[0].stored.evidence.provenance.evidence, /digests match/);
  });

  it("recoverStored reports digest drift and expired handles", async () => {
    const storage = memStorage();
    saveStoredRun(storage, storedRun());
    const drifted = storedRun();
    drifted.arms = [{ variant: "baseline", groupId: "grp-gone", resolvedId: "res-baseline", runDigest: "dig-b" }];
    saveStoredRun(storage, drifted);
    const fetchImpl = stubFetch([
      ["GET", "/v1/runs/grp-faulted", async () => jsonResponse(200, groupBody("grp-faulted", "res-faulted", "run-f", "CHANGED"))],
      ["GET", "/v1/runs/grp-gone", async () => jsonResponse(404, { error: "not_found", detail: "unknown run group 'grp-gone'" })],
    ]);
    const api = new ServiceGuideApi({ live: fakeLiveTraffic(), fetchImpl, storage });
    const outcomes = await api.recoverStored();
    assert.equal(outcomes.length, 2);
    assert.equal(outcomes[0].verified, false);
    assert.match(outcomes[0].problems.join(" "), /digest changed/);
    assert.equal(outcomes[1].verified, false);
    assert.match(outcomes[1].problems.join(" "), /expired/);
  });
});
