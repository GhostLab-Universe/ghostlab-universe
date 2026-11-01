/**
 * Browser tests of the API build through the same-origin proxy, against M07's real code
 * in tests/integration/outcome_server.py. Service-backed, with scripted inference: not a
 * live result.
 */
import { randomUUID } from "node:crypto";
import type { Page } from "@playwright/test";
import { eventsCompared, expect, noSidewaysScroll, shot, test, view } from "./support.ts";

const REFUND = "refund-world/1";
const JOURNEY = "group-journey/1";
const POLLED = { timeout: 30_000 };

async function start(page: Page, world: string, marker = "") {
  await page.goto("/#/");
  await expect(page.locator(".session")).toBeVisible();
  await page.getByLabel("World").selectOption(world);
  await page.getByLabel("Objective").fill(`Find a failure after a fault ${marker} ${randomUUID()}`.trim());
  await page.getByLabel("Success criterion").fill("A failing invariant is reproduced");
  await page.getByRole("button", { name: "Start campaign" }).click();
  await expect(page).toHaveURL(/#\/campaign\/c-/, POLLED);
}

const approval = (page: Page) => view(page, /Approval/);
const approve = (page: Page) => page.getByRole("button", { name: "Approve this action" }).click();
const paused = (page: Page) => expect(page.getByTestId("state-awaiting_approval")).toBeVisible(POLLED);

/** The last steps of the priority flow: a capsule of the verified repair, then its replay. */
async function replayRepaired(page: Page) {
  await view(page, "Capsule");
  await page.getByLabel("Run").selectOption("repaired");
  await page.getByRole("button", { name: "Export capsule" }).click();
  await expect(page.getByRole("button", { name: "Replay capsule" })).toBeEnabled();
  await page.getByRole("button", { name: "Replay capsule" }).click();
  await expect(page.getByTestId("replay-verdict")).toContainText("Reproduced");
  await expect(page.getByTestId("replay-fields")).toContainText("run_status");
  await expect(page.getByTestId("replay-fields")).toContainText("completed");
  await expect(page.getByTestId("replay-fields")).toContainText("model_calls");
}

test("the session shows the scripted inference and has no test data banner", async ({ page }) => {
  await page.goto("/#/");
  const session = page.locator(".session");
  await expect(session).toContainText("auth_mode local_dev");
  await expect(session).toContainText("production_authenticated false");
  await expect(session).toContainText("inference fixture");
  await expect(page.locator(".service-mode")).toContainText("scripted test data, not a model");
  await expect(page.getByTestId("test-data-banner")).toHaveCount(0);
  await shot(page, "service-home");
});

test("refund end to end: create, evidence, diff, replay, download, approve", async ({ page }) => {
  await start(page, REFUND);
  await paused(page);
  await shot(page, "service-refund-campaign");

  await view(page, "Evidence");
  await expect(page.getByTestId("event-2")).toContainText("PaymentTimeout");
  await expect(page.getByText("run_completed").first()).toBeVisible();
  await shot(page, "service-refund-evidence");

  await view(page, "Reality Diff");
  await expect(page.getByTestId("first-divergence")).toContainText("Event 2");
  await expect(page.getByText("Total refunded 500000 minor units")).toBeVisible();
  await expect(page.getByTestId("coverage")).toContainText("complete true");
  await eventsCompared(page);
  await shot(page, "service-refund-diff");

  await view(page, "Capsule");
  await page.getByRole("button", { name: "Export capsule" }).click();
  await page.getByRole("button", { name: "Replay capsule" }).click();
  await expect(page.getByTestId("replay-verdict")).toContainText("Failure reproduced");
  await expect(page.getByTestId("replay-fields")).toContainText("model_calls");
  const downloading = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download" }).click();
  const download = await downloading;
  expect(await download.failure()).toBeNull();

  await approval(page);
  await expect(page.getByTestId("exact-action")).toContainText('"adapter": "repaired"');
  await page.getByLabel("Reason (optional)").fill("Reviewed the diff");
  await shot(page, "service-refund-approval");
  await approve(page);
  await expect(page.getByTestId("repair-verdict")).toContainText("Repair verified", POLLED);
  await expect(page.getByTestId("repair-verdict")).toHaveAttribute("data-tone", "ok");
  await expect(page.getByRole("button", { name: "Approve this action" })).toHaveCount(0);
  await shot(page, "service-refund-repaired");

  await page.reload();
  await expect(page.getByTestId("repair-verdict")).toContainText("Repair verified", POLLED);
  await replayRepaired(page);
  await shot(page, "service-refund-repaired-replay");
});

test("journey end to end: both events, enforced repair held by the adapter", async ({ page }) => {
  await start(page, JOURNEY);
  await paused(page);
  await view(page, "Reality Diff");
  const first = page.getByTestId("first-divergence");
  const violating = page.getByTestId("violating-events");
  await expect(first).toContainText("Event 10");
  await expect(violating).toContainText("Event 12");
  await expect(violating).toContainText("different event from the first divergence");
  await eventsCompared(page);
  await shot(page, "service-journey-diff");
  await violating.getByRole("link").first().click();
  await expect(page.locator(".events > li:focus")).toBeInViewport();
  await expect(page.locator(".events > li:focus")).toContainText("Violation evidence");
  await shot(page, "service-journey-evidence");

  await approval(page);
  await expect(page.getByTestId("execution-mode")).toContainText("enforced");
  await shot(page, "service-journey-approval");
  await approve(page);
  await expect(page.getByTestId("repair-verdict")).toContainText("Repair verified", POLLED);
  await expect(page.getByTestId("repair-fields")).toContainText("adapter");
  await shot(page, "service-journey-repaired");
  await replayRepaired(page);
  await shot(page, "service-journey-repaired-replay");
});

test("rejecting: 'completed' does not read as success", async ({ page }) => {
  await start(page, REFUND);
  await paused(page);
  await approval(page);
  await page.getByRole("button", { name: "Reject" }).click();
  await expect(page.locator(".status-line")).toContainText("repair_rejected", POLLED);
  await expect(page.locator(".status-line")).toContainText("Stopped: completed");
  await expect(page.locator('[data-tone="ok"]')).toHaveCount(0);
});

test("coverage_incomplete: campaign, diff and replay", async ({ page }) => {
  await start(page, JOURNEY, "outcome-truncated");
  await expect(page.locator(".status-line")).toContainText("coverage_incomplete", POLLED);
  await expect(page.locator(".status-line")).toContainText("Inconclusive");
  await expect(page.getByTestId("state-incomplete")).toBeVisible();
  await expect(page.getByTestId("coverage").first()).toContainText("complete false");
  await expect(page.locator('[data-tone="ok"]')).toHaveCount(0);
  await shot(page, "service-coverage-incomplete-campaign");
  await view(page, "Reality Diff");
  await expect(page.getByTestId("state-incomplete")).toBeVisible();
  await shot(page, "service-coverage-incomplete-diff");
  await view(page, "Capsule");
  await page.getByRole("button", { name: "Export capsule" }).click();
  await page.getByRole("button", { name: "Replay capsule" }).click();
  await expect(page.getByTestId("replay-verdict")).toContainText("stopped early");
  await shot(page, "service-coverage-incomplete-replay");
});

test("a fault that never fired is never clean, and says why", async ({ page }) => {
  await start(page, REFUND, "outcome-silent-fault");
  await expect(page.locator(".status-line")).toContainText("coverage_incomplete", POLLED);
  await expect(page.getByTestId("coverage").first()).toContainText("faults_triggered false");
  await expect(page.getByTestId("synthetic-label")).toBeVisible();
  await expect(page.locator('[data-tone="ok"]')).toHaveCount(0);
  await view(page, "Reality Diff");
  await expect(page.getByTestId("state-incomplete")).toContainText("Both runs finished, but a fault never fired");
  await shot(page, "service-coverage-unfired-diff");
});

test("no_experiment_ran and budget_check_failed are named failures", async ({ page }) => {
  await start(page, REFUND, "outcome-no-experiment");
  await expect(page.getByTestId("state-failed")).toContainText("No experiment ran", POLLED);
  await expect(page.locator(".status-line")).toContainText("no_experiment_ran");
  await shot(page, "service-no-experiment");
  await start(page, JOURNEY, "outcome-budget-check");
  await expect(page.getByTestId("state-failed")).toContainText("Budget check failed", POLLED);
  await shot(page, "service-budget-check-failed");
});

test("a repair that missed a fault shows lost_faults", async ({ page }) => {
  await start(page, JOURNEY, "outcome-lost-fault");
  await paused(page);
  await approval(page);
  await approve(page);
  await expect(page.getByTestId("repair-verdict")).toContainText("Repair incomplete", POLLED);
  await expect(page.getByTestId("repair-fields")).toContainText("sharing_revoked_while_queued");
  await expect(page.locator('[data-tone="ok"]')).toHaveCount(0);
  await shot(page, "service-repair-lost-fault");
  await page.goto(`${page.url().split("/approval")[0]}/evidence?run=repaired`);
  await expect(page.getByText("This repair missed a fault")).toBeVisible(POLLED);
  await view(page, "Capsule");
  await page.getByLabel("Run").selectOption("repaired");
  await page.getByRole("button", { name: "Export capsule" }).click();
  await page.getByRole("button", { name: "Replay capsule" }).click();
  await expect(page.getByTestId("replay-verdict")).toContainText("missed a fault");
});

test("cancelling a paused campaign ends it; a later decision moves nothing", async ({ page }) => {
  await start(page, REFUND);
  await paused(page);
  await page.getByRole("button", { name: "Cancel campaign" }).click();
  await expect(page.locator(".status-line")).toContainText("Cancelled", POLLED);
  await approval(page);
  await expect(page.getByText("This campaign has stopped")).toBeVisible();
  await approve(page);
  const notice = page.getByText("Decision recorded, campaign not resumed").locator("..");
  await expect(notice).toContainText("campaign_finished", POLLED);
  await expect(page.getByRole("button", { name: "Resume campaign" })).toHaveCount(0);
  await expect(page.getByTestId("repair-verdict")).toHaveCount(0);
  await shot(page, "service-decision-on-stopped");
});

test("cancel_incomplete says the campaign did not stop, and its status stays", async ({ page }) => {
  await start(page, REFUND);
  await paused(page);
  const refused = JSON.stringify({
    reason: "cancel_incomplete",
    error: { code: "conflict", message: "The campaign did not stop", ref: null, retryable: false, side_effect: "none" },
  });
  await page.route("**/cancel", (route) => route.fulfill({ status: 409, contentType: "application/json", body: refused }));
  await page.getByRole("button", { name: "Cancel campaign" }).click();
  await expect(page.getByTestId("state-conflict")).toContainText("cancel_incomplete", POLLED);
  await expect(page.getByTestId("state-conflict")).toContainText("The campaign did not stop");
  await expect(page.locator(".status-line")).toContainText("Awaiting approval");
});

test("a violation before truncation reaches approval", async ({ page }) => {
  await start(page, JOURNEY, "outcome-violation");
  await paused(page);
  await view(page, "Reality Diff");
  await expect(page.getByTestId("state-incomplete")).toContainText("violation recorded");
  await expect(page.getByTestId("violating-events")).toContainText("Event 10");
  await expect(page.getByTestId("leak-story")).toContainText("read the withdrawal at event 6 and delivered anyway");
  await approval(page);
  await expect(page.getByTestId("state-incomplete")).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve this action" })).toBeVisible();
  await shot(page, "service-truncated-violation-approval");
});

test("repair_incomplete: a truncated repair never looks like success", async ({ page }) => {
  await start(page, JOURNEY, "outcome-short-repair");
  await paused(page);
  await approval(page);
  await approve(page);
  const verdict = page.getByTestId("repair-verdict");
  await expect(verdict).toContainText("Repair incomplete", POLLED);
  await expect(verdict).toHaveAttribute("data-tone", "warn");
  await expect(page.getByTestId("repair-fields")).toContainText("budget_exhausted");
  await expect(page.getByTestId("repair-fields")).toContainText("not set");
  await expect(page.locator('[data-tone="ok"]')).toHaveCount(0);
  await shot(page, "service-repair-incomplete");
});

test("settings_changed at consumption: blocked, unspent, no resume", async ({ page }) => {
  await start(page, JOURNEY, "outcome-settings-at-consume");
  await paused(page);
  await approval(page);
  await approve(page);
  await expect(page.locator(".status-line")).toContainText("Blocked: settings changed", POLLED);
  await expect(page.getByTestId("state-blocked")).toBeVisible();
  await expect(page.getByRole("button", { name: "Resume campaign" })).toHaveCount(0);
  await shot(page, "service-settings-ended-approval");
  await view(page, "Campaign");
  await expect(page.getByTestId("state-blocked")).toBeVisible();
  await expect(page.getByTestId("state-failed")).toHaveCount(0);
  await shot(page, "service-settings-ended-campaign");
});

test("a resume refused by settings_changed is blocked, then restoring finishes it", async ({ page }) => {
  await start(page, JOURNEY, "outcome-settings-at-resume");
  await paused(page);
  await approval(page);
  await approve(page);
  await expect(page.getByTestId("state-blocked")).toBeVisible(POLLED);
  await expect(page.getByTestId("state-blocked")).toContainText("settings_changed");
  await shot(page, "service-settings-refused");

  await page.reload();
  await expect(page.getByText("Approved, not yet used")).toBeVisible(POLLED);
  const refused = JSON.stringify({
    reason: "settings_changed",
    error: { code: "conflict", message: "still refused", ref: null, retryable: false, side_effect: "none" },
  });
  await page.route("**/resume", (route) => route.fulfill({ status: 409, contentType: "application/json", body: refused }), { times: 1 });
  await page.getByRole("button", { name: "Resume campaign" }).click();
  await expect(page.getByTestId("state-blocked")).toContainText("still refused", POLLED);
  await page.getByRole("button", { name: "Resume campaign" }).click();
  await expect(page.getByTestId("repair-verdict")).toContainText("Repair verified", POLLED);
  await expect(page.getByTestId("state-blocked")).toHaveCount(0);
});

test("a withdrawn binding is a refusal, then restoring it finishes the repair", async ({ page }) => {
  await start(page, JOURNEY, "outcome-binding-at-resume");
  await paused(page);
  await approval(page);
  await approve(page);
  const blocked = page.getByTestId("state-blocked");
  await expect(blocked).toContainText("the world identity no longer owns this", POLLED);
  await expect(blocked).toContainText("a refusal, not a failed repair");
  await expect(page.getByTestId("repair-verdict")).toHaveCount(0);
  await shot(page, "service-binding-withdrawn");
  await page.getByRole("button", { name: "Resume campaign" }).click();
  await expect(page.getByTestId("repair-verdict")).toContainText("Repair verified", POLLED);
});

test("credential-shaped text is refused with the service's reason", async ({ page }) => {
  await page.goto("/#/");
  await page.getByLabel("Objective").fill("Check token=abcd1234 handling");
  await page.getByLabel("Success criterion").fill("Anything");
  await page.getByRole("button", { name: "Start campaign" }).click();
  await expect(page.getByTestId("state-validation_error")).toContainText("secret_shaped_text");
  await expect(page).toHaveURL(/#\/$/);
});

test("reconnecting holds the last status, then re-reads it", async ({ page }) => {
  await start(page, REFUND);
  await paused(page);
  await page.route("**/v1/**", (route) => route.abort());
  await expect(page.getByTestId("state-reconnecting")).toBeVisible(POLLED);
  await expect(page.locator(".status-line")).toContainText("Awaiting approval");
  await shot(page, "service-reconnecting");
  await page.unroute("**/v1/**");
  await expect(page.getByTestId("state-reconnecting")).toHaveCount(0, POLLED);
});

test("keyboard: start, navigate and decide with no pointer", async ({ page }) => {
  await page.goto("/#/");
  await expect(page.locator(".session")).toBeVisible();
  await page.getByLabel("Objective").focus();
  await page.keyboard.type(`Find a failure by keyboard ${randomUUID()}`);
  await page.keyboard.press("Tab");
  await page.keyboard.type("A failing invariant is reproduced");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/#\/campaign\/c-/, POLLED);
  await paused(page);
  await page.getByRole("navigation", { name: "Campaign views" }).getByRole("link", { name: /Approval/ }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1 })).toBeFocused();
  await page.getByRole("button", { name: "Approve this action" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("repair-verdict")).toContainText("Repair verified", POLLED);
});

for (const [name, pattern] of [
  ["approval", "**/v1/approvals/**"],
  ["Reality Diff", "**/experiments/**"],
] as const) {
  test(`the ${name} view recovers when the service returns`, async ({ page }) => {
    await start(page, REFUND);
    await paused(page);
    await page.route(pattern, (route) => route.abort());
    await view(page, name === "approval" ? /Approval/ : name);
    await expect(page.getByTestId("state-reconnecting").first()).toBeVisible(POLLED);
    await page.unroute(pattern);
    await expect(page.getByTestId(name === "approval" ? "exact-action" : "first-divergence")).toBeVisible(POLLED);
  });
}

test("an unknown campaign is not found", async ({ page }) => {
  await page.goto("/#/campaign/does-not-exist");
  await expect(page.getByTestId("state-not_found")).toBeVisible();
});

test("a foreign id in the URL is handled as data", async ({ page }) => {
  await page.goto("/#/campaign/%3Cimg%20src%3Dx%3E");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("<img src=x>");
  await expect(page.locator("main img")).toHaveCount(0);
});

test.describe("narrow screen", () => {
  test.use({ viewport: { width: 320, height: 640 } });

  test("the journey flow works at this width", async ({ page }) => {
    await start(page, JOURNEY);
    await paused(page);
    await noSidewaysScroll(page);
    await shot(page, "service-narrow-journey-campaign");
    await view(page, "Reality Diff");
    await expect(page.getByTestId("violating-events")).toBeVisible();
    await eventsCompared(page);
    await noSidewaysScroll(page);
    await shot(page, "service-narrow-journey-diff");
    await approval(page);
    await approve(page);
    await expect(page.getByTestId("repair-verdict")).toContainText("Repair verified", POLLED);
    await noSidewaysScroll(page);
  });
});
