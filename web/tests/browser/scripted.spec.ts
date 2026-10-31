/**
 * The demo, in Chrome, against the real `python -m ghostlab.api --scripted` server: real
 * worlds, verifier, approvals, stores and capsules. Proposals are M07's checked-in ones,
 * never a model, so this is service-backed and not a live or integrated result.
 */
import type { Page } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { eventsCompared, expect, noSidewaysScroll, shot, test, view } from "./support.ts";

const POLLED = { timeout: 30_000 };
const NOT_A_MODEL = "Proposals scripted: checked-in replies, not a model";

async function start(page: Page, world: string, scenario: string) {
  await page.goto("/#/");
  await expect(page.locator(".session")).toContainText("inference scripted");
  await page.getByLabel("World").selectOption(world);
  await page.getByLabel(/Scripted scenario/).selectOption(scenario);
  await page.getByLabel("Objective").fill(`Find what ${scenario} shows ${randomUUID()}`);
  await page.getByLabel("Success criterion").fill("A failing invariant is reproduced");
  await page.getByRole("button", { name: "Start campaign" }).click();
  await expect(page).toHaveURL(/#\/campaign\/c-/, POLLED);
}

const paused = (page: Page) => expect(page.getByTestId("state-awaiting_approval")).toBeVisible(POLLED);

test("the demo: a careful agent leaks, the boundary blocks it, replay needs no model", async ({ page }) => {
  await start(page, "group-journey/1", "revocation_race");
  await paused(page);
  await expect(page.getByTestId("campaign-inference")).toContainText(NOT_A_MODEL);
  await expect(page.getByTestId("synthetic-label")).toBeVisible();
  await expect(page.locator(".service-mode")).toContainText("not a model");
  await shot(page, "demo-1-campaign");

  await view(page, "Reality Diff");
  await eventsCompared(page);
  const story = page.getByTestId("leak-story");
  await expect(story).toContainText("participant-c lost sharing at event 18; msg-3 was delivered to them at event 18.");
  await expect(story).toContainText("allowed at event 17");
  await expect(story).toContainText("participant-c");
  await expect(page.getByTestId("divergence-cause")).toContainText("participant-c lost sharing at event 18");
  await shot(page, "demo-2-diff");
  await page.getByTestId("leak-story").getByRole("link", { name: "Event 18" }).click();
  await expect(page.getByTestId("delivery-18")).toContainText("Delivered msg-3 to participant-c");
  await expect(page.getByTestId("delivery-18")).toContainText("false");
  await shot(page, "demo-3-evidence");

  await view(page, "Capsule");
  await page.getByRole("button", { name: "Export capsule" }).click();
  await page.getByRole("button", { name: "Replay capsule" }).click();
  await expect(page.getByTestId("replay-verdict")).toContainText("Failure reproduced");
  await expect(page.getByTestId("replay-fields")).toContainText(/model_calls\s*0/);
  await shot(page, "demo-4-replay");

  await view(page, /Approval/);
  await expect(page.getByTestId("execution-mode")).toContainText("enforced");
  await shot(page, "demo-5-approval");
  await page.getByRole("button", { name: "Approve this action" }).click();
  await expect(page.getByTestId("repair-verdict")).toContainText("Repair verified", POLLED);
  await expect(page.getByTestId("repair-verdict")).toHaveAttribute("data-tone", "ok");
  await expect(page.getByTestId("repair-fields")).toContainText("guarded_boundary");
  await expect(page.getByText("stays unspent")).toHaveCount(0);
  await expect(page.getByText("This campaign has stopped")).toHaveCount(0);
  await expect(page.getByTestId("decision-region")).toBeFocused();
  await shot(page, "demo-6-repaired");

  await page.goto(`${page.url().split("/approval")[0]}/evidence?run=repaired`);
  await expect(page.getByTestId("delivery-18")).toContainText("Denied msg-3 to participant-c");
  await expect(page.getByTestId("event-18")).toContainText("PermissionWithdrawnDenied");
  await shot(page, "demo-7-enforced");

  await view(page, "Capsule");
  await page.getByLabel("Run").selectOption("repaired");
  await page.getByRole("button", { name: "Export capsule" }).click();
  await page.getByRole("button", { name: "Replay capsule" }).click();
  await expect(page.getByTestId("replay-fields")).toContainText(/model_calls\s*0/);
  await expect(page.getByTestId("replay-verdict")).toContainText("Reproduced");
});

test("the default journey names the delivery that broke the rule", async ({ page }) => {
  await start(page, "group-journey/1", "default");
  await paused(page);
  await view(page, "Reality Diff");
  await eventsCompared(page);
  await expect(page.getByTestId("leak-story")).toContainText(
    "participant-c lost sharing at event 10; msg-3 was delivered to them at event 12.",
  );
  await expect(page.getByTestId("divergence-cause")).toContainText("not the violation");
  await shot(page, "scripted-journey-diff");
});

for (const [world, name] of [
  ["group-journey/1", "journey"],
  ["refund-world/1", "refund"],
] as const) {
  test(`an experiment whose fault never fired is inconclusive, in ${name}`, async ({ page }) => {
    await start(page, world, "fault_never_fires");
    await expect(page.locator(".status-line")).toContainText("coverage_incomplete", POLLED);
    await expect(page.locator(".status-line")).toContainText("Inconclusive");
    await expect(page.locator(".status-line")).not.toContainText("Budget exhausted");
    await expect(page.getByTestId("state-incomplete")).toContainText("proves nothing");
    await expect(page.getByTestId("coverage").first()).toContainText("faults_triggered false");
    await expect(page.getByText("Scripted proposal calls")).toBeVisible();
    await expect(page.getByText("Model calls")).toHaveCount(0);
    await expect(page.locator('[data-tone="ok"]')).toHaveCount(0);
    await shot(page, `demo-8-inconclusive-${name}`);
    await view(page, "Capsule");
    await page.getByRole("button", { name: "Export capsule" }).click();
    await page.getByRole("button", { name: "Replay capsule" }).click();
    const verdict = page.getByTestId("replay-verdict");
    await expect(verdict).toContainText("this run tested nothing");
    await expect(verdict).toHaveAttribute("data-tone", "warn");
    await expect(page.getByText("every check it records passed")).toHaveCount(0);
    await shot(page, `demo-9-inconclusive-replay-${name}`);
  });
}

test("the refund flow runs end to end on the same server", async ({ page }) => {
  await start(page, "refund-world/1", "default");
  await paused(page);
  await view(page, "Evidence");
  await expect(page.getByTestId("event-2")).toContainText("PaymentTimeout");
  await view(page, "Reality Diff");
  await eventsCompared(page);
  await expect(page.getByTestId("first-divergence")).toContainText("Event 2");
  await expect(page.getByText("Total refunded 500000 minor units")).toBeVisible();
  await view(page, /Approval/);
  await page.getByRole("button", { name: "Approve this action" }).click();
  await expect(page.getByTestId("repair-verdict")).toContainText("Repair verified", POLLED);
  await view(page, "Capsule");
  await page.getByLabel("Run").selectOption("repaired");
  await page.getByRole("button", { name: "Export capsule" }).click();
  await page.getByRole("button", { name: "Replay capsule" }).click();
  await expect(page.getByTestId("replay-fields")).toContainText(/model_calls\s*0/);
  await expect(page.getByTestId("replay-verdict")).toContainText("Reproduced");
});

test("rejecting on the same server leaves nothing unspent to warn about", async ({ page }) => {
  await start(page, "refund-world/1", "default");
  await paused(page);
  await view(page, /Approval/);
  await page.getByRole("button", { name: "Reject" }).click();
  await expect(page.locator(".status-line")).toContainText("repair_rejected", POLLED);
  await expect(page.getByText("stays unspent")).toHaveCount(0);
  await expect(page.getByText("This campaign has stopped")).toHaveCount(0);
  await expect(page.getByTestId("repair-verdict")).toHaveCount(0);
});

test("a scenario a world doesn't have is refused with the service's reason", async ({ page }) => {
  await start(page, "refund-world/1", "revocation_race").catch(() => undefined);
  await expect(page.getByTestId("state-validation_error")).toContainText("scripted_scenario_unavailable");
});

test.describe("narrow screen", () => {
  test.use({ viewport: { width: 320, height: 640 } });

  test("the race reads the same at 320px", async ({ page }) => {
    await start(page, "group-journey/1", "revocation_race");
    await paused(page);
    await view(page, "Reality Diff");
    await eventsCompared(page);
    await expect(page.getByTestId("leak-story")).toContainText("msg-3 was delivered to them at event 18");
    await noSidewaysScroll(page);
    await shot(page, "demo-narrow-diff");
  });
});
