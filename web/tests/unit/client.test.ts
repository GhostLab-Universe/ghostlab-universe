import assert from "node:assert/strict";
import { test } from "node:test";
import { createApiClient } from "../../src/api/client.ts";
import { OPERATIONS } from "../../src/api/operations.generated.ts";

interface Seen {
  url: string;
  method: string;
  headers: Record<string, string>;
  credentials: string | undefined;
  body: string | undefined;
}

function stub(status: number, body: unknown, seen: Seen[] = [], text?: string) {
  const fetch = async (url: string | URL | Request, init?: RequestInit) => {
    seen.push({
      url: String(url),
      method: init?.method ?? "GET",
      headers: init?.headers as Record<string, string>,
      credentials: init?.credentials,
      body: init?.body as string | undefined,
    });
    return new Response(text ?? JSON.stringify(body), { status });
  };
  return fetch as typeof globalThis.fetch;
}

const CAMPAIGN = Object.fromEntries(OPERATIONS.getCampaign.required.map((key) => [key, null]));

test("campaign and environment operations have client paths; account/source use dedicated components", () => {
  assert.deepEqual(Object.keys(OPERATIONS).filter(id => !["getAuthStatus", "startSignIn", "completeSignIn", "signOut", "inspectGitHubSource"].includes(id)).sort(), [
    "assessExtFidelity",
    "cancelCampaign",
    "createCampaign",
    "createExpConfig",
    "decideApproval",
    "diffExpResolved",
    "downloadArtifact",
    "exportCapsule",
    "getApproval",
    "getCampaign",
    "getExpConfig",
    "getExpPolicy",
    "getExpResolved",
    "getExpRun",
    "getExperiment",
    "getExtEnvironment",
    "getExtFixture",
    "getRealityDiff",
    "getRun",
    "getSession",
    "importCapsule",
    "inspectExtCapsule",
    "listCampaignEvents",
    "listExpConfigs",
    "listExpResolved",
    "listExpRuns",
    "listExtEnvironments",
    "minimizeExtFaults",
    "qualifyExtension",
    "replayCampaignCapsule",
    "replayCapsule",
    "replayExtRecorded",
    "resolveExpConfig",
    "resumeCampaign",
    "retestExtFresh",
    "runExpResolved",
    "runExtCampaign",
  ]);
});

test("no request parameter can name a project, principal, role or scope", () => {
  for (const [id, operation] of Object.entries(OPERATIONS)) {
    const names = `${operation.path} ${(operation.query as readonly string[]).join(" ")}`;
    assert.doesNotMatch(names, /project|principal|role|scope/i, id);
  }
});

test("reads go to the contract's path, escaped, with no client identity", async () => {
  const seen: Seen[] = [];
  const client = createApiClient({ fetch: stub(200, CAMPAIGN, seen) });
  const result = await client.getCampaign("a/b c");
  assert.ok(result.ok);
  assert.equal(seen[0]?.url, "/v1/campaigns/a%2Fb%20c");
  assert.equal(seen[0]?.method, "GET");
  assert.equal(seen[0]?.credentials, "same-origin");
  assert.deepEqual(Object.keys(seen[0]?.headers ?? {}), ["Accept"]);
  assert.equal(seen[0]?.body, undefined);
});

test("experiment runs post to the resolved id and diffs compare by query", async () => {
  const seen: Seen[] = [];
  const runBody = Object.fromEntries(OPERATIONS.runExpResolved.required.map((key) => [key, null]));
  const client = createApiClient({ fetch: stub(200, runBody, seen) });
  const ran = await client.runExpResolved("res-1", { repetition: 2, attempt: 1 });
  assert.ok(ran.ok);
  assert.equal(seen[0]?.url, "/v1/extensions/exp-resolved/res-1/run");
  assert.equal(seen[0]?.method, "POST");
  assert.deepEqual(JSON.parse(seen[0]?.body ?? "{}"), { repetition: 2, attempt: 1 });
  const diffBody = Object.fromEntries(OPERATIONS.diffExpResolved.required.map((key) => [key, null]));
  const seenDiff: Seen[] = [];
  const diffClient = createApiClient({ fetch: stub(200, diffBody, seenDiff) });
  const compared = await diffClient.diffExpResolved("res-1", "res-2");
  assert.ok(compared.ok);
  assert.equal(seenDiff[0]?.url, "/v1/extensions/exp-diff?left_id=res-1&right_id=res-2");
  const listBody = Object.fromEntries(OPERATIONS.listExpRuns.required.map((key) => [key, null]));
  const seenList: Seen[] = [];
  const listClient = createApiClient({ fetch: stub(200, listBody, seenList) });
  const listed = await listClient.listExpRuns();
  assert.ok(listed.ok);
  assert.equal(seenList[0]?.url, "/v1/extensions/exp-runs");
});

test("events page by the sequence cursor", async () => {
  const seen: Seen[] = [];
  const page = { campaign_id: "c", events: [], next_after: 7, has_more: false, inference: "fixture" };
  const client = createApiClient({ fetch: stub(200, page, seen) });
  const result = await client.listEvents("c", 7);
  assert.ok(result.ok);
  assert.equal(seen[0]?.url, "/v1/campaigns/c/events?after=7");
});

test("a decision posts only the submission, with its Idempotency-Key", async () => {
  const seen: Seen[] = [];
  const body = Object.fromEntries(OPERATIONS.decideApproval.required.map((key) => [key, null]));
  const client = createApiClient({ fetch: stub(200, body, seen) });
  const submission = { schema_version: 1 as const, approval_id: "approval-1", action_digest: "a".repeat(64), decision: "approve" as const, reason: null };
  const result = await client.decideApproval(submission, "decision-1");
  assert.ok(result.ok);
  assert.equal(seen[0]?.url, "/v1/approvals/approval-1/decision");
  assert.equal(seen[0]?.method, "POST");
  assert.equal(seen[0]?.headers["Idempotency-Key"], "decision-1");
  assert.deepEqual(JSON.parse(seen[0]?.body ?? ""), submission);
});

test("a scripted scenario travels as the contract's query, and never otherwise", async () => {
  const seen: Seen[] = [];
  const client = createApiClient({ fetch: stub(202, CAMPAIGN, seen) });
  await client.createCampaign({} as never, "key-1", "revocation_race");
  await client.createCampaign({} as never, "key-2");
  assert.equal(seen[0]?.url, "/v1/campaigns?scripted_scenario=revocation_race");
  assert.equal(seen[1]?.url, "/v1/campaigns");
  assert.deepEqual(OPERATIONS.createCampaign.queryValues.scripted_scenario, ["default", "revocation_race", "fault_never_fires"]);
});

test("an error body keeps the service's code and reason", async () => {
  const body = {
    reason: "awaiting_approval",
    error: { code: "conflict", message: "Nothing is running", ref: null, retryable: false, side_effect: "none" },
  };
  const client = createApiClient({ fetch: stub(409, body) });
  const result = await client.cancelCampaign("c");
  assert.equal(result.ok, false);
  if (!result.ok && result.failure.kind === "service") {
    assert.equal(result.failure.status, 409);
    assert.equal(result.failure.reason, "awaiting_approval");
    assert.equal(result.failure.error.code, "conflict");
  } else {
    assert.ok(false, "expected a service failure");
  }
});

test("a network error is a transport failure, which the UI treats as reconnecting", async () => {
  const fetch = (async () => {
    throw new TypeError("Failed to fetch");
  }) as typeof globalThis.fetch;
  const result = await createApiClient({ fetch }).getSession();
  assert.equal(result.ok, false);
  if (!result.ok) assert.equal(result.failure.kind, "transport");
});

test("a dead proxy is a transport failure, not a service verdict", async () => {
  const result = await createApiClient({ fetch: stub(503, null, [], "upstream down") }).getSession();
  assert.equal(result.ok, false);
  if (!result.ok) assert.equal(result.failure.kind, "transport");
});

test("a success that is missing contract fields is not accepted", async () => {
  const result = await createApiClient({ fetch: stub(200, { record: {} }) }).getCampaign("c");
  assert.equal(result.ok, false);
  if (!result.ok) {
    assert.equal(result.failure.kind, "invalid_response");
    if (result.failure.kind === "invalid_response") assert.match(result.failure.message, /event_count/);
  }
});

test("a success status the contract doesn't name is not accepted", async () => {
  const result = await createApiClient({ fetch: stub(200, CAMPAIGN) }).createCampaign({} as never, "key-1");
  assert.equal(result.ok, false);
  if (!result.ok) assert.equal(result.failure.kind, "invalid_response");
});

test("a body that is not an error response is never read as a verdict", async () => {
  const result = await createApiClient({ fetch: stub(418, { approved: true }) }).getCampaign("c");
  assert.equal(result.ok, false);
  if (!result.ok) assert.equal(result.failure.kind, "invalid_response");
});

test("artifacts come back as bytes", async () => {
  const fetch = (async () => new Response("capsule-bytes", { status: 200 })) as typeof globalThis.fetch;
  const result = await createApiClient({ fetch }).downloadArtifact("artifact-1");
  assert.ok(result.ok);
  assert.equal(await result.data.text(), "capsule-bytes");
});
