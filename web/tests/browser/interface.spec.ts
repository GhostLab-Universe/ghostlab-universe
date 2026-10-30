/**
 * Browser tests for keyboard use, narrow screens and the full click-through.
 *
 * NOT YET RUN. @playwright/test is not a dependency, so these have never executed and
 * prove nothing until M00 adds it. They are excluded from tsconfig for the same reason.
 * Once it is installed, from web/: `npx playwright test -c tests/browser`.
 *
 * Everything runs in fixture mode, so a pass here is a fixture result, not an integrated
 * one. The integrated flow is tests/integration/m07-flow.test.tsx.
 */
import { expect, test, type Page } from "@playwright/test";

const open = (page: Page, path: string) => page.goto(`/#/${path}`);
const view = (page: Page, name: string) =>
  page.getByRole("navigation", { name: "Campaign views" }).getByRole("link", { name }).click();

test.beforeEach(async ({ page }) => {
  await open(page, "");
});

test("the test data banner is on every page and cannot be dismissed", async ({ page }) => {
  await expect(page.getByTestId("test-data-banner")).toBeVisible();
  await open(page, "campaign/fx-refund/diff");
  await expect(page.getByTestId("test-data-banner")).toBeVisible();
  await expect(page.getByTestId("test-data-banner").getByRole("button")).toHaveCount(0);
});

test("refund flow: diff, replay, approve, verified repair", async ({ page }) => {
  await open(page, "campaign/fx-refund");
  await expect(page.getByTestId("state-awaiting_approval")).toBeVisible();

  await view(page, "Reality Diff");
  await expect(page.getByTestId("first-divergence")).toContainText("Event 2");
  await expect(page.getByText("Total refunded 500000 minor units")).toBeVisible();

  await view(page, "Capsule");
  await page.getByRole("button", { name: "Export capsule" }).click();
  await page.getByRole("button", { name: "Replay capsule" }).click();
  await expect(page.getByTestId("replay-verdict")).toContainText("Failure reproduced");
  await expect(page.getByTestId("replay-fields")).toContainText("model_calls");

  await view(page, "Approval");
  await expect(page.getByTestId("exact-action")).toContainText('"adapter": "repaired"');
  await page.getByRole("button", { name: "Approve this action" }).click();
  await expect(page.getByTestId("repair-verdict")).toContainText("Repair verified");
  await expect(page.getByRole("button", { name: "Approve this action" })).toHaveCount(0);
});

test("journey flow: both events are identified and the evidence opens", async ({ page }) => {
  await open(page, "campaign/fx-journey/diff");
  await expect(page.getByTestId("first-divergence")).toContainText("Event 10");
  await expect(page.getByTestId("violating-events")).toContainText("Event 12");
  await page.getByTestId("violating-events").getByRole("link", { name: "Event 12" }).click();
  await expect(page.getByTestId("event-12")).toBeFocused();
  await expect(page.getByTestId("event-12")).toContainText("Violation evidence");

  await view(page, "Approval");
  await expect(page.getByTestId("execution-mode")).toContainText("enforced");
  await page.getByRole("button", { name: "Approve this action" }).click();
  await expect(page.getByTestId("repair-fields")).toContainText("adapter");
});

test("repair_incomplete never looks like success", async ({ page }) => {
  await open(page, "campaign/fx-journey-truncated");
  const verdict = page.getByTestId("repair-verdict");
  await expect(verdict).toContainText("Repair incomplete");
  await expect(verdict).toHaveAttribute("data-tone", "warn");
  await expect(page.getByTestId("repair-fields")).toContainText("budget_exhausted");
});

test("a denied decision stays denied", async ({ page }) => {
  await open(page, "campaign/fx-approval-denied/approval");
  await page.getByRole("button", { name: "Approve this action" }).click();
  await expect(page.getByTestId("state-denied")).toBeVisible();
  await page.reload();
  await expect(page.getByRole("button", { name: "Approve this action" })).toBeVisible();
  await expect(page.getByTestId("repair-verdict")).toHaveCount(0);
});

test("reconnecting holds the last status, then re-reads it", async ({ page }) => {
  await open(page, "campaign/fx-running");
  await page.getByRole("button", { name: "Drop connection" }).click();
  await expect(page.getByTestId("state-reconnecting")).toBeVisible();
  await page.getByRole("button", { name: "Restore connection" }).click();
  await expect(page.getByTestId("state-reconnecting")).toHaveCount(0);
});

test("states: loading, denied, validation, not found, cancelled, failed, stalled", async ({ page }) => {
  await open(page, "campaign/fx-slow");
  await expect(page.getByTestId("state-loading")).toBeVisible();
  for (const [id, state] of [
    ["fx-load-denied", "denied"],
    ["fx-load-invalid", "validation_error"],
    ["fx-load-missing", "not_found"],
    ["fx-cancelled", "cancelled"],
    ["fx-failed", "failed"],
    ["fx-stalled", "failed"],
    ["fx-empty/evidence", "empty"],
  ] as const) {
    await open(page, `campaign/${id}`);
    await expect(page.getByTestId(`state-${state}`).first()).toBeVisible();
  }
});

test("keyboard: skip link, view navigation and the decision need no pointer", async ({ page }) => {
  await open(page, "campaign/fx-refund");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("main")).toBeFocused();

  const approval = page.getByRole("navigation", { name: "Campaign views" }).getByRole("link", { name: /Approval/ });
  await approval.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1 })).toBeFocused();

  const approve = page.getByRole("button", { name: "Approve this action" });
  await approve.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("repair-verdict")).toContainText("Repair verified");
});

test("every interactive element shows focus", async ({ page }) => {
  await open(page, "campaign/fx-refund/approval");
  for (let press = 0; press < 12; press += 1) {
    await page.keyboard.press("Tab");
    const outline = await page.evaluate(() => {
      const element = document.activeElement;
      return element === null ? "none" : getComputedStyle(element).outlineStyle;
    });
    expect(outline).not.toBe("none");
  }
});

test.describe("narrow screen", () => {
  test.use({ viewport: { width: 320, height: 640 } });

  for (const path of ["", "campaign/fx-refund", "campaign/fx-journey/diff", "campaign/fx-journey/evidence", "campaign/fx-journey/approval"]) {
    test(`no horizontal page scroll: /${path}`, async ({ page }) => {
      await open(page, path);
      await expect(page.getByTestId("test-data-banner")).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow).toBeLessThanOrEqual(0);
    });
  }
});
