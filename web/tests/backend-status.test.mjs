// Unit tests for the B5 backend status surface (web/src/backend-status.ts).
// Runs on stock Node via type stripping; the service is a stubbed fetch.
//   node --experimental-strip-types --test web/tests/backend-status.test.mjs

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  DEFAULT_BACKEND_NAME,
  buildFailureModel,
  buildReadyModel,
  fetchBackendList,
  fetchBackendStatus,
  fetchGroupJob,
  formatBackendHeadline,
  formatEvidenceCompleteness,
  formatIsolation,
  formatJobState,
  formatKnownBackends,
  formatLimits,
  formatLiveness,
  formatPackageIdentity,
  formatQualification,
  formatTermination,
  isLiveServiceClient,
  parseBackendDescriptor,
  parseBackendFailure,
  parseBackendList,
  parseBackendStatus,
  parseGroupJob,
  renderBackendStatus,
  serviceGet,
} from "../src/backend-status.ts";

function trustedDescriptor() {
  return {
    name: "trusted-local",
    version: "backends/1",
    trusted_local: true,
    isolation: "in-process-checked-in-local",
    isolation_scope: "none: runs in the controller process",
    limitations: ["no OS/process boundary"],
    available: true,
    unavailable_reason: null,
    capabilities: {
      execute_runs: true,
      cancel: true,
      lifecycle: ["starting", "running", "completed"],
      network: "in-process",
      model_access: "controller-side only",
    },
    qualification: {
      status: "trusted-local (explicit; not an isolation boundary)",
      qualified_for_isolation: false,
      detail: "allowlist + digest + budgets",
      checks: [],
    },
    limits: {
      enforced: false,
      cpu: null,
      memory: null,
      pids: null,
      wall_clock: null,
      output: null,
      detail: "controller budgets only",
    },
    package_identity: {
      kind: "checked-in-adapter",
      image: null,
      detail: "allowlisted module:Class + digest",
    },
  };
}

function containerDescriptor() {
  const descriptor = trustedDescriptor();
  descriptor.name = "linux-container";
  descriptor.trusted_local = false;
  descriptor.isolation = "container (planned; no runtime qualified)";
  descriptor.available = false;
  descriptor.unavailable_reason = "no runtime qualified; refusing";
  descriptor.qualification = {
    status: "unqualified-for-isolation",
    qualified_for_isolation: false,
    detail: "suite has not passed",
    checks: [{ name: "canary-read", passed: false, detail: "leaked" }],
  };
  return descriptor;
}

function statusPayload() {
  return {
    project_id: "proj-b5",
    backend: trustedDescriptor(),
    live_liveness: {
      backend: "trusted-local",
      reachable: true,
      checked_at: "2026-11-03T00:00:00Z",
      detail: "in-process path",
    },
  };
}

function groupPayload() {
  return {
    project_id: "proj-b5",
    group_id: "grp-abc",
    resolved_id: "res-1",
    status: "completed",
    attempt: 0,
    backend: { name: "trusted-local", trusted_local: true, isolation: "in-process-checked-in-local" },
    job: { job_id: "job-1", state: "completed" },
    termination: { reason: "completed", detail: null },
    runs: [
      { run_id: "run-1", resolved_id: "res-1", repetition: 0, attempt: 0, digest: "d", created_at: "t",
        run: { status: "completed", verdict: "fail", model_calls: 0, tool_calls: 3, wall_clock_ms: 1,
               error: null, evidence: { snapshot: {} }, checks: {} } },
    ],
    error: null,
    created_at: "t",
    updated_at: "t",
  };
}

function stubFetch(routes) {
  return async (url, init) => {
    const path = String(url).replace("https://svc.test", "");
    assert.equal(init.headers["X-GhostLab-Project"], "proj-b5");
    const hit = routes[path];
    assert.ok(hit !== undefined, `unexpected fetch path ${path}`);
    return { status: hit.status, text: async () => JSON.stringify(hit.body) };
  };
}

/** Find React element nodes in a rendered tree by data-testid. */
function findByTestId(node, testid, out = []) {
  if (node === null || node === undefined || typeof node !== "object") return out;
  if (Array.isArray(node)) {
    for (const child of node) findByTestId(child, testid, out);
    return out;
  }
  const props = node.props ?? {};
  if (props["data-testid"] === testid) out.push(node);
  findByTestId(props.children, testid, out);
  return out;
}

function textOf(node) {
  if (typeof node === "string") return node;
  if (node === null || node === undefined || typeof node !== "object") return "";
  if (Array.isArray(node)) return node.map(textOf).join("");
  return textOf(node.props?.children);
}

describe("parseBackendDescriptor", () => {
  it("parses the trusted-local descriptor", () => {
    const parsed = parseBackendDescriptor(trustedDescriptor());
    assert.equal(parsed.name, "trusted-local");
    assert.equal(parsed.trustedLocal, true);
    assert.equal(parsed.available, true);
    assert.equal(parsed.unavailableReason, null);
    assert.equal(parsed.qualification.qualifiedForIsolation, false);
    assert.deepEqual(parsed.limitations, ["no OS/process boundary"]);
    assert.equal(parsed.limits.enforced, false);
    assert.equal(parsed.packageIdentity.image, null);
  });

  it("rejects a descriptor missing qualification", () => {
    const bad = trustedDescriptor();
    delete bad.qualification;
    assert.throws(() => parseBackendDescriptor(bad), /malformed/);
  });

  it("rejects non-boolean availability", () => {
    const bad = trustedDescriptor();
    bad.available = "yes";
    assert.throws(() => parseBackendDescriptor(bad), /malformed/);
  });
});

describe("parseBackendList / parseBackendStatus", () => {
  it("parses the list with both backends", () => {
    const parsed = parseBackendList({ project_id: "p", backends: [trustedDescriptor(), containerDescriptor()] });
    assert.equal(parsed.projectId, "p");
    assert.equal(parsed.backends.length, 2);
    assert.equal(parsed.backends[1].available, false);
    assert.equal(parsed.backends[1].qualification.checks.length, 1);
  });

  it("parses status with the live liveness probe", () => {
    const parsed = parseBackendStatus(statusPayload());
    assert.equal(parsed.projectId, "proj-b5");
    assert.equal(parsed.backend.name, "trusted-local");
    assert.equal(parsed.liveness.reachable, true);
    assert.equal(parsed.liveness.checkedAt, "2026-11-03T00:00:00Z");
  });

  it("rejects status without liveness", () => {
    const bad = statusPayload();
    delete bad.live_liveness;
    assert.throws(() => parseBackendStatus(bad), /malformed/);
  });
});

describe("parseBackendFailure", () => {
  it("captures the 503 refusal shape", () => {
    const failure = parseBackendFailure(503, { error: "backend_unavailable", detail: "refusing" });
    assert.deepEqual(failure, { status: 503, error: "backend_unavailable", detail: "refusing" });
  });

  it("returns null for non-refusal bodies", () => {
    assert.equal(parseBackendFailure(503, null), null);
    assert.equal(parseBackendFailure(503, { error: "x" }), null);
    assert.equal(parseBackendFailure(200, { ok: true }), null);
  });
});

describe("parseGroupJob", () => {
  it("reads job/termination/evidence from a completed group", () => {
    const job = parseGroupJob(groupPayload());
    assert.equal(job.groupId, "grp-abc");
    assert.equal(job.backendName, "trusted-local");
    assert.equal(job.jobId, "job-1");
    assert.equal(job.jobState, "completed");
    assert.equal(job.terminationReason, "completed");
    assert.equal(job.runsRecorded, 1);
    assert.equal(job.runsWithEvidence, 1);
    assert.equal(job.truncated, null);
    assert.deepEqual(job.missing, []);
  });

  it("derives the termination reason from a terminal status", () => {
    const payload = groupPayload();
    payload.termination = null;
    payload.job = null;
    payload.backend = null;
    const job = parseGroupJob(payload);
    assert.equal(job.terminationReason, "completed");
    assert.equal(job.jobId, null);
    assert.equal(job.backendName, null);
  });

  it("leaves running groups unterminated", () => {
    const payload = groupPayload();
    payload.status = "running";
    payload.termination = null;
    payload.runs = [];
    const job = parseGroupJob(payload);
    assert.equal(job.terminationReason, null);
    assert.equal(job.runsRecorded, 0);
  });

  it("reads truncated/missing flags when stated", () => {
    const payload = groupPayload();
    payload.truncated = true;
    payload.missing = ["trace page 2"];
    const job = parseGroupJob(payload);
    assert.equal(job.truncated, true);
    assert.deepEqual(job.missing, ["trace page 2"]);
  });
});

describe("formatters", () => {
  it("labels trusted-local explicitly, never as a sandbox", () => {
    const headline = formatBackendHeadline(parseBackendDescriptor(trustedDescriptor()));
    assert.match(headline, /trusted-local/);
    assert.match(headline, /not a sandbox/);
    assert.doesNotMatch(headline, /sandbox[^)]*available/i);
  });

  it("marks the unavailable backend with its reason", () => {
    const headline = formatBackendHeadline(parseBackendDescriptor(containerDescriptor()));
    assert.match(headline, /UNAVAILABLE/);
    assert.match(headline, /no runtime qualified/);
  });

  it("formats qualification, isolation, package and liveness", () => {
    const descriptor = parseBackendDescriptor(trustedDescriptor());
    assert.match(formatQualification(descriptor), /qualified_for_isolation=no/);
    assert.match(formatIsolation(descriptor), /in-process-checked-in-local/);
    assert.match(formatPackageIdentity(descriptor), /checked-in-adapter/);
    const status = parseBackendStatus(statusPayload());
    assert.match(formatLiveness(status.liveness), /reachable/);
  });

  it("renders limits as honest effective lines", () => {
    const lines = formatLimits(parseBackendDescriptor(trustedDescriptor()));
    assert.ok(lines.some((line) => line.includes("enforced: no")));
    assert.ok(lines.some((line) => line.includes("cpu: not set")));
    assert.ok(lines.some((line) => line.startsWith("network:")));
  });

  it("formats job state, termination and evidence completeness", () => {
    const job = parseGroupJob(groupPayload());
    assert.match(formatJobState(job), /job job-1: completed/);
    assert.match(formatTermination(job), /^completed/);
    assert.match(formatEvidenceCompleteness(job), /1 run\(s\) recorded, 1 with evidence/);
    assert.match(formatEvidenceCompleteness(job), /truncation not stated/);
  });

  it("flags truncation and missing evidence", () => {
    const payload = groupPayload();
    payload.truncated = true;
    payload.missing = ["trace page 2"];
    payload.runs[0].run.evidence = null;
    const job = parseGroupJob(payload);
    assert.match(formatEvidenceCompleteness(job), /TRUNCATED/);
    assert.match(formatEvidenceCompleteness(job), /missing: trace page 2/);
    assert.match(formatEvidenceCompleteness(job), /0 with evidence/);
  });

  it("summarizes known backends", () => {
    const list = parseBackendList({ project_id: "p", backends: [trustedDescriptor(), containerDescriptor()] });
    assert.equal(formatKnownBackends(list.backends), "trusted-local (available), linux-container (unavailable)");
    assert.equal(formatKnownBackends([]), "none reported");
  });
});

describe("buildFailureModel + renderBackendStatus", () => {
  it("renders the 503-shaped refusal as a failure state", () => {
    const model = buildFailureModel(503, { error: "backend_unavailable", detail: "daemon down; refusing" }, "linux-container");
    assert.equal(model.kind, "failure");
    const tree = renderBackendStatus(model);
    const panel = findByTestId(tree, "backend-status-panel");
    assert.equal(panel.length, 1);
    const failure = findByTestId(tree, "backend-failure");
    assert.equal(failure.length, 1);
    assert.match(textOf(tree), /linux-container/);
    assert.match(textOf(tree), /without fallback/);
    assert.match(textOf(tree), /daemon down/);
  });

  it("refuses to mislabel unexpected refusals as backend failures", () => {
    assert.throws(() => buildFailureModel(404, { error: "not_found", detail: "nope" }, "ghost"), /unexpected status/);
    assert.throws(() => buildFailureModel(503, { ok: true }, "ghost"), /unexpected status/);
  });

  it("renders a ready model with backend, job and evidence rows", () => {
    const model = buildReadyModel(
      statusPayload(),
      { project_id: "p", backends: [trustedDescriptor(), containerDescriptor()] },
      groupPayload(),
    );
    assert.equal(model.kind, "ready");
    const tree = renderBackendStatus(model);
    assert.equal(findByTestId(tree, "backend-status-panel").length, 1);
    assert.equal(findByTestId(tree, "backend-failure").length, 0);
    const text = textOf(tree);
    assert.match(text, /trusted-local/);
    assert.match(text, /job job-1: completed/);
    assert.match(text, /1 run\(s\) recorded/);
  });

  it("renders ready without a run and renders loading/error states", () => {
    const model = buildReadyModel(statusPayload(), { project_id: "p", backends: [trustedDescriptor()] }, null);
    assert.match(textOf(renderBackendStatus(model)), /no run selected/);
    assert.match(textOf(renderBackendStatus({ kind: "loading" })), /Loading backend status/);
    assert.match(textOf(renderBackendStatus({ kind: "error", message: "boom" })), /boom/);
  });
});

describe("isLiveServiceClient", () => {
  it("accepts the service adapter shape and refuses fixture/transport shapes", () => {
    assert.equal(isLiveServiceClient({ mode: "live", serviceBaseUrl: "", projectId: () => "p" }), true);
    assert.equal(isLiveServiceClient({ mode: "fixture-scripted" }), false);
    assert.equal(isLiveServiceClient({ mode: "live" }), false);
    assert.equal(isLiveServiceClient({ mode: "live", serviceBaseUrl: "" }), false);
  });
});

describe("serviceGet / fetch helpers", () => {
  const ref = { baseUrl: "https://svc.test", projectId: "proj-b5" };

  it("sends the project header and parses JSON", async () => {
    const fetchImpl = stubFetch({ "/v1/backends": { status: 200, body: { a: 1 } } });
    const result = await serviceGet({ ...ref, fetchImpl }, "/v1/backends");
    assert.deepEqual(result, { status: 200, body: { a: 1 } });
  });

  it("rejects non-JSON bodies", async () => {
    const fetchImpl = async () => ({ status: 200, text: async () => "not json{" });
    await assert.rejects(() => serviceGet({ ...ref, fetchImpl }, "/v1/backends"), /non-JSON/);
  });

  it("fetches the backend list", async () => {
    const body = { project_id: "proj-b5", backends: [trustedDescriptor()] };
    const fetchImpl = stubFetch({ "/v1/backends": { status: 200, body } });
    const list = await fetchBackendList({ ...ref, fetchImpl });
    assert.equal(list.backends.length, 1);
    assert.equal(list.backends[0].name, DEFAULT_BACKEND_NAME);
  });

  it("fetches backend status and surfaces 503 as failure, not a throw", async () => {
    const okImpl = stubFetch({ "/v1/backends/trusted-local/status": { status: 200, body: statusPayload() } });
    const ok = await fetchBackendStatus({ ...ref, fetchImpl: okImpl }, "trusted-local");
    assert.equal(ok.httpStatus, 200);
    assert.equal(ok.status.backend.name, "trusted-local");
    assert.equal(ok.failure, null);

    const downImpl = stubFetch({
      "/v1/backends/linux-container/status": { status: 503, body: { error: "backend_unavailable", detail: "down" } },
    });
    const down = await fetchBackendStatus({ ...ref, fetchImpl: downImpl }, "linux-container");
    assert.equal(down.httpStatus, 503);
    assert.equal(down.status, null);
    assert.deepEqual(down.failure, { status: 503, error: "backend_unavailable", detail: "down" });
  });

  it("throws on unexpected status failures", async () => {
    const fetchImpl = stubFetch({ "/v1/backends/ghost/status": { status: 404, body: { error: "not_found", detail: "nope" } } });
    await assert.rejects(() => fetchBackendStatus({ ...ref, fetchImpl }, "ghost"), /404/);
  });

  it("fetches the raw group job view", async () => {
    const fetchImpl = stubFetch({ "/v1/runs/grp-abc": { status: 200, body: groupPayload() } });
    const job = await fetchGroupJob({ ...ref, fetchImpl }, "grp-abc");
    assert.equal(job.jobId, "job-1");
    assert.equal(job.terminationReason, "completed");
  });
});
