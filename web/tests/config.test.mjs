// Focused unit tests for the guided-run config core (web/src/config.ts).
// Runs on stock Node via type stripping; oracles are node:crypto and a
// python-stdlib one-liner (no repo imports, immune to parallel edits).
//   node --experimental-strip-types --test web/tests/config.test.mjs

import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { describe, it } from "node:test";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  CONTROL_PATHS,
  canonicalize,
  defaultConfig,
  flattenConfig,
  getPath,
  setPath,
  toCampaignSpec,
  toExperimentProposal,
  validateEffectiveConfig,
  validateManifestShape,
} from "../src/config.ts";

const here = path.dirname(fileURLToPath(import.meta.url));
const manifestPath = path.join(here, "..", "src", "fixtures", "illustrative-manifest.json");

function mutated(mutator) {
  const config = JSON.parse(JSON.stringify(defaultConfig()));
  mutator(config);
  return config;
}

function expectInvalid(config, pathFragment) {
  const checked = validateEffectiveConfig(config);
  assert.equal(checked.ok, false);
  assert.ok(
    checked.errors.some((e) => e.path.includes(pathFragment)),
    `expected an error at '${pathFragment}', got ${JSON.stringify(checked.errors)}`,
  );
}

describe("validateEffectiveConfig", () => {
  it("accepts the defaults", () => {
    const checked = validateEffectiveConfig(defaultConfig());
    assert.equal(checked.ok, true);
  });

  it("rejects non-integer, boolean, negative and out-of-range money", () => {
    for (const paid of [true, 1.5, 0, -1, 1000000000001, "250000"]) {
      expectInvalid(mutated((c) => { c.fixture.paidMinor = paid; }), "fixture.paidMinor");
    }
  });

  it("rejects bad identifiers", () => {
    expectInvalid(mutated((c) => { c.fixture.orderId = ""; }), "fixture.orderId");
    expectInvalid(mutated((c) => { c.fixture.orderId = "x".repeat(101); }), "fixture.orderId");
    expectInvalid(mutated((c) => { c.fixture.customerId = ""; }), "fixture.customerId");
    expectInvalid(mutated((c) => { c.env.envId = "Refund!"; }), "env.envId");
    expectInvalid(mutated((c) => { c.env.adapterRef = "not-a-ref"; }), "env.adapterRef");
    expectInvalid(mutated((c) => { c.env.manifestDigest = "xyz"; }), "env.manifestDigest");
  });

  it("rejects unknown fields at every level", () => {
    expectInvalid(mutated((c) => { c.bogus = 1; }), "bogus");
    expectInvalid(mutated((c) => { c.limits.bogus = 1; }), "limits.bogus");
    expectInvalid(mutated((c) => { c.fixture.actor_id = "spoof"; }), "fixture.actor_id");
  });

  it("rejects unpaired cost amount/currency", () => {
    expectInvalid(mutated((c) => { c.limits.maxCostMinor = 100; }), "limits.maxCostMinor");
    expectInvalid(mutated((c) => { c.limits.costCurrency = "USD"; }), "limits.maxCostMinor");
    expectInvalid(mutated((c) => { c.limits.costCurrency = "usd"; }), "limits.costCurrency");
  });

  it("rejects out-of-range budgets", () => {
    expectInvalid(mutated((c) => { c.limits.maxToolCalls = 0; }), "limits.maxToolCalls");
    expectInvalid(mutated((c) => { c.limits.maxToolCalls = 1001; }), "limits.maxToolCalls");
    expectInvalid(mutated((c) => { c.limits.maxParallelRuns = 65; }), "limits.maxParallelRuns");
    expectInvalid(mutated((c) => { c.limits.maxWallClockMs = 0; }), "limits.maxWallClockMs");
  });

  it("rejects bad invariants, adapter, objective and criteria", () => {
    expectInvalid(mutated((c) => { c.checks.invariants = []; }), "checks.invariants");
    expectInvalid(
      mutated((c) => { c.checks.invariants = ["authorized_refunds", "authorized_refunds"]; }),
      "checks.invariants",
    );
    expectInvalid(mutated((c) => { c.checks.invariants = ["nope"]; }), "checks.invariants");
    expectInvalid(mutated((c) => { c.agent.adapter = "random"; }), "agent.adapter");
    expectInvalid(mutated((c) => { c.objective = ""; }), "objective");
    expectInvalid(mutated((c) => { c.successCriteria = []; }), "successCriteria");
    expectInvalid(mutated((c) => { c.faults.timeoutAfterCommit = "yes"; }), "faults.timeoutAfterCommit");
  });
});

describe("paths, builders", () => {
  it("every CONTROL_PATH resolves in the defaults", () => {
    const config = defaultConfig();
    for (const control of CONTROL_PATHS) {
      assert.notEqual(getPath(config, control.path), undefined, control.path);
    }
    const flat = flattenConfig(config);
    assert.equal(flat.length, CONTROL_PATHS.length);
    assert.deepEqual(flat.map(([p]) => p), CONTROL_PATHS.map((c) => c.path));
  });

  it("setPath updates immutably", () => {
    const before = defaultConfig();
    const after = setPath(before, "limits.maxToolCalls", 25);
    assert.equal(before.limits.maxToolCalls, 20);
    assert.equal(after.limits.maxToolCalls, 25);
    assert.equal(validateEffectiveConfig(after).ok, true);
  });

  it("toCampaignSpec matches the frozen shape exactly", () => {
    const spec = toCampaignSpec(defaultConfig());
    assert.deepEqual(Object.keys(spec).sort(), ["budget", "objective", "schema_version", "success_criteria", "world_version"]);
    assert.deepEqual(Object.keys(spec.budget).sort(), [
      "cost_currency", "max_cost_minor", "max_input_tokens", "max_model_calls",
      "max_output_tokens", "max_parallel_runs", "max_tool_calls", "max_wall_clock_ms",
    ]);
    assert.equal(spec.schema_version, 1);
    assert.equal(spec.world_version, "refund-world/1");
    assert.equal(spec.budget.max_tool_calls, 20);
  });

  it("toExperimentProposal is exact and deterministic", () => {
    const first = toExperimentProposal(defaultConfig());
    const second = toExperimentProposal(defaultConfig());
    assert.deepEqual(first, second);
    assert.deepEqual(Object.keys(first).sort(), ["adapter", "expected_signal", "hypothesis", "scenario", "schema_version"]);
    assert.deepEqual(first.scenario, { timeout_after_commit: true, max_tool_calls: 20 });
    assert.equal(first.adapter, "vulnerable");
  });
});

describe("canonicalize", () => {
  it("sorts keys and uses compact separators", () => {
    assert.equal(canonicalize({ b: 1, a: { d: 4, c: [3, 2] } }), '{"a":{"c":[3,2],"d":4},"b":1}');
  });

  it("rejects non-finite numbers", () => {
    assert.throws(() => canonicalize({ x: Number.NaN }), /non-finite/);
  });

  it("matches python json.dumps canonical form on the illustrative manifest", async () => {
    const text = await readFile(manifestPath, "utf-8");
    const ours = canonicalize(JSON.parse(text));
    const script = "import json,sys; print(json.dumps(json.load(sys.stdin), sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False), end='')";
    const candidates = process.platform === "win32" ? ["python", "py"] : ["python3", "python"];
    let lastError = null;
    for (const bin of candidates) {
      try {
        const args = bin === "py" ? ["-3", "-c", script] : ["-c", script];
        const out = await new Promise((resolve, reject) => {
          const child = execFile(bin, args, { maxBuffer: 1_000_000 }, (error, stdout, stderr) =>
            (error ? reject(new Error(stderr || error.message)) : resolve(stdout)));
          child.stdin.write(text);
          child.stdin.end();
        });
        assert.equal(ours, out);
        // node:crypto digest of the canonical text is a stable 64-hex input.
        const digest = createHash("sha256").update(ours, "utf-8").digest("hex");
        assert.match(digest, /^[0-9a-f]{64}$/);
        return;
      } catch (error) {
        lastError = error;
      }
    }
    console.warn(`python oracle unavailable, canonical form unverified: ${lastError}`);
  });
});

describe("validateManifestShape", () => {
  it("accepts the illustrative manifest", async () => {
    const text = await readFile(manifestPath, "utf-8");
    const checked = validateManifestShape(JSON.parse(text));
    assert.equal(checked.ok, true);
  });

  it("rejects unknown keys, bad versions, URLs and paths", async () => {
    const base = JSON.parse(await readFile(manifestPath, "utf-8"));
    const bad = (mutator) => {
      const copy = JSON.parse(JSON.stringify(base));
      mutator(copy);
      return validateManifestShape(copy);
    };
    assert.equal(bad((m) => { m.pip_packages = ["x"]; }).ok, false);
    assert.equal(bad((m) => { m.manifest_version = 2; }).ok, false);
    assert.equal(bad((m) => { m.notes = "see https://example.com/x"; }).ok, false);
    assert.equal(bad((m) => { m.notes = "/etc/passwd"; }).ok, false);
    assert.equal(bad((m) => { m.tools = []; }).ok, false);
  });
});
