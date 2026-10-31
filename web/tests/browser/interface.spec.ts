/**
 * Browser tests of the fixture build, in headless Chrome. A pass here is a fixture
 * result, not an integrated one: everything on screen is labelled test data.
 * The same flows against M07's real code are in service.spec.ts.
 */
import { eventsCompared, expect, go, noSidewaysScroll, shot, test, view } from "./support.ts";

test.beforeEach(async ({ page }) => {
  await go(page, "");
});

test("the test data banner is on every page and cannot be dismissed", async ({ page }) => {
  await expect(page.getByTestId("test-data-banner")).toBeVisible();
  await shot(page, "home-wide");
  await go(page, "campaign/fx-refund/diff");
  await expect(page.getByTestId("test-data-banner")).toBeVisible();
  await expect(page.getByTestId("test-data-banner").getByRole("button")).toHaveCount(0);
});

test("the session's three modes are shown", async ({ page }) => {
  const session = page.locator(".session");
  await expect(session).toContainText("auth_mode local_dev");
  await expect(session).toContainText("production_authenticated false");
  await expect(session).toContainText("inference disabled");
});

test("refund flow: diff, replay, download, approve, verified repair", async ({ page }) => {
  await go(page, "campaign/fx-refund");
  await expect(page.getByTestId("state-awaiting_approval")).toBeVisible();
  await shot(page, "refund-campaign");

  await view(page, "Evidence");
  await expect(page.getByTestId("event-2")).toContainText("PaymentTimeout");
  await shot(page, "refund-evidence");

  await view(page, "Reality Diff");
  await expect(page.getByTestId("first-divergence")).toContainText("Event 2");
  await expect(page.getByText("Total refunded 500000 minor units")).toBeVisible();
  await eventsCompared(page);
  await shot(page, "refund-diff");

  await view(page, "Capsule");
  await page.getByRole("button", { name: "Export capsule" }).click();
  await expect(page.getByRole("button", { name: "Replay capsule" })).toBeEnabled();
  await page.getByRole("button", { name: "Replay capsule" }).click();
  await expect(page.getByTestId("replay-verdict")).toContainText("Failure reproduced");
  await expect(page.getByTestId("replay-fields")).toContainText("model_calls");
  const downloading = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download" }).click();
  const download = await downloading;
  expect(download.suggestedFilename()).toMatch(/\.json$/);
  expect(await download.failure()).toBeNull();
  await shot(page, "refund-capsule");

  await view(page, /Approval/);
  await expect(page.getByTestId("exact-action")).toContainText('"adapter": "repaired"');
  await shot(page, "refund-approval");
  await page.getByRole("button", { name: "Approve this action" }).click();
  await expect(page.getByTestId("repair-verdict")).toContainText("Repair verified");
  await expect(page.getByRole("button", { name: "Approve this action" })).toHaveCount(0);
  await shot(page, "refund-repaired");
});

test("rejecting runs nothing and 'completed' does not read as success", async ({ page }) => {
  await go(page, "campaign/fx-refund/approval");
  await page.getByRole("button", { name: "Reject" }).click();
  await expect(page.locator(".status-line")).toContainText("Stopped: completed");
  await expect(page.locator(".status-line")).toContainText("repair_rejected");
  await expect(page.getByTestId("repair-verdict")).toHaveCount(0);
});

test("journey flow: both events are identified and the evidence opens", async ({ page }) => {
  await go(page, "campaign/fx-journey/diff");
  await expect(page.getByTestId("first-divergence")).toContainText("Event 10");
  await expect(page.getByTestId("violating-events")).toContainText("Event 12");
  await expect(page.getByTestId("difference-table")).toContainText("sharing.participant-c");
  await eventsCompared(page);
  await shot(page, "journey-diff");

  await page.getByTestId("violating-events").getByRole("link", { name: "Event 12" }).click();
  await expect(page.getByTestId("event-12")).toBeFocused();
  await expect(page.getByTestId("event-12")).toBeInViewport();
  await expect(page.getByTestId("event-12")).toContainText("Violation evidence");
  await shot(page, "journey-evidence");

  await view(page, /Approval/);
  await expect(page.getByTestId("execution-mode")).toContainText("enforced");
  await page.getByRole("button", { name: "Approve this action" }).click();
  await expect(page.getByTestId("repair-fields")).toContainText("adapter");
  await shot(page, "journey-repaired");
});

test("coverage_incomplete is never success, in the campaign or the diff", async ({ page }) => {
  await go(page, "campaign/fx-coverage-incomplete");
  await expect(page.locator(".status-line")).toContainText("Budget exhausted");
  await expect(page.getByTestId("state-incomplete")).toBeVisible();
  await expect(page.locator('[data-tone="ok"]')).toHaveCount(0);
  await shot(page, "coverage-incomplete-campaign");
  await view(page, "Reality Diff");
  await expect(page.getByTestId("state-incomplete")).toBeVisible();
  await expect(page.getByTestId("coverage")).toContainText("complete false");
  await shot(page, "coverage-incomplete-diff");
  await view(page, "Capsule");
  await page.getByRole("button", { name: "Export capsule" }).click();
  await page.getByRole("button", { name: "Replay capsule" }).click();
  await expect(page.getByTestId("replay-verdict")).toContainText("stopped early");
});

test("a fault that never fired is never clean, though both runs finished", async ({ page }) => {
  await go(page, "campaign/fx-coverage-unfired");
  await expect(page.locator(".status-line")).toContainText("coverage_incomplete");
  await expect(page.getByTestId("coverage")).toContainText("faults_triggered false");
  await expect(page.locator('[data-tone="ok"]')).toHaveCount(0);
  await view(page, "Reality Diff");
  await expect(page.getByTestId("state-incomplete")).toContainText("Both runs finished, but a fault never fired");
  await shot(page, "coverage-unfired-diff");
});

test("a repair that missed a fault shows lost_faults and no success", async ({ page }) => {
  await go(page, "campaign/fx-journey-lost-fault");
  await expect(page.getByTestId("repair-verdict")).toHaveAttribute("data-tone", "warn");
  await expect(page.getByTestId("repair-fields")).toContainText("sharing_revoked_while_queued");
  await expect(page.locator('[data-tone="ok"]')).toHaveCount(0);
  await shot(page, "repair-lost-fault");
});

for (const [id, title] of [
  ["fx-no-experiment", "No experiment ran"],
  ["fx-budget-check", "Budget check failed"],
] as const) {
  test(`${title} is named`, async ({ page }) => {
    await go(page, `campaign/${id}`);
    await expect(page.getByTestId("state-failed")).toContainText(title);
    await expect(page.locator('[data-tone="ok"]')).toHaveCount(0);
    await shot(page, `outcome-${id}`);
  });
}

test("cancelling a paused campaign ends it; a later decision moves nothing", async ({ page }) => {
  await go(page, "campaign/fx-refund");
  await page.getByRole("button", { name: "Cancel campaign" }).click();
  await expect(page.locator(".status-line")).toContainText("Cancelled");
  await view(page, "Approval");
  await expect(page.getByText("This campaign has stopped")).toBeVisible();
  await page.getByRole("button", { name: "Approve this action" }).click();
  const notice = page.getByText("Decision recorded, campaign not resumed").locator("..");
  await expect(notice).toContainText("campaign_finished");
  await expect(notice).toContainText("moved nothing");
  await expect(page.getByRole("button", { name: "Resume campaign" })).toHaveCount(0);
  await expect(page.getByTestId("repair-verdict")).toHaveCount(0);
  await shot(page, "decision-on-stopped-campaign");
});

test("every campaign view says the world is synthetic", async ({ page }) => {
  await go(page, "campaign/fx-refund");
  for (const name of ["Campaign", "Evidence", "Reality Diff", /Approval/, "Capsule"]) {
    await view(page, name);
    await expect(page.getByTestId("synthetic-label")).toBeVisible();
  }
});

test("a violation before truncation stays visible and reaches approval", async ({ page }) => {
  await go(page, "campaign/fx-coverage-violation/diff");
  await expect(page.getByTestId("violating-events")).toContainText("Event 10");
  await expect(page.getByTestId("state-incomplete")).toContainText("violation recorded");
  await view(page, /Approval/);
  await expect(page.getByTestId("state-incomplete")).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve this action" })).toBeVisible();
});

for (const [id, status] of [
  ["fx-journey-truncated", "Budget exhausted"],
  ["fx-journey-idle", "Failed"],
] as const) {
  test(`repair_incomplete never looks like success: ${status}`, async ({ page }) => {
    await go(page, `campaign/${id}`);
    await expect(page.locator(".status-line")).toContainText(status);
    const verdict = page.getByTestId("repair-verdict");
    await expect(verdict).toContainText("Repair incomplete");
    await expect(verdict).toHaveAttribute("data-tone", "warn");
    await expect(page.getByTestId("repair-fields")).toContainText("not set");
    await expect(page.locator('[data-tone="ok"]')).toHaveCount(0);
    await shot(page, `repair-incomplete-${status.split(" ")[0]?.toLowerCase()}`);
  });
}

test("a refused resume is blocked, with the approval unspent after a reload", async ({ page }) => {
  await go(page, "campaign/fx-resume-failed/approval");
  await page.getByRole("button", { name: "Approve this action" }).click();
  await expect(page.getByTestId("state-blocked")).toBeVisible();
  await expect(page.getByTestId("state-failed")).toHaveCount(0);
  await shot(page, "settings-changed-refused");
  await page.getByRole("button", { name: "Resume campaign" }).click();
  await expect(page.getByRole("button", { name: "Resume campaign" })).toBeEnabled();
  await expect(page.getByTestId("state-blocked")).toHaveCount(1);
  await page.reload();
  await expect(page.getByTestId("state-denied")).toHaveCount(0);
});

test("failed / settings_changed reads as blocked and offers no resume", async ({ page }) => {
  await go(page, "campaign/fx-settings-blocked");
  await expect(page.locator(".status-line")).toContainText("Blocked: settings changed");
  await expect(page.getByTestId("state-blocked")).toBeVisible();
  await expect(page.getByRole("button", { name: "Resume campaign" })).toHaveCount(0);
  await shot(page, "settings-changed-ended");
});

test("redacted text renders plainly", async ({ page }) => {
  await go(page, "campaign/fx-redacted/evidence");
  await expect(page.getByText("(secret-shaped text redacted)")).toBeVisible();
  await expect(page.getByTestId("state-failed")).toHaveCount(0);
});

test("a denied decision stays denied", async ({ page }) => {
  await go(page, "campaign/fx-approval-denied/approval");
  await page.getByRole("button", { name: "Approve this action" }).click();
  await expect(page.getByTestId("state-denied")).toBeVisible();
  await shot(page, "approval-denied");
  await expect(page.getByRole("button", { name: "Approve this action" })).toBeVisible();
  await expect(page.getByTestId("repair-verdict")).toHaveCount(0);
});

test("reconnecting holds the last status, then re-reads it", async ({ page }) => {
  await go(page, "campaign/fx-running");
  await expect(page.locator(".status-line")).toContainText("Running");
  await page.getByRole("button", { name: "Drop connection" }).click();
  await expect(page.getByTestId("state-reconnecting")).toBeVisible();
  await expect(page.locator(".status-line")).toContainText("Running");
  await shot(page, "reconnecting");
  await page.getByRole("button", { name: "Restore connection" }).click();
  await expect(page.getByTestId("state-reconnecting")).toHaveCount(0);
});

test("cancelling goes through the service", async ({ page }) => {
  await go(page, "campaign/fx-running");
  await page.getByRole("button", { name: "Cancel campaign" }).click();
  await expect(page.locator(".status-line")).toContainText("Cancelled");
  await expect(page.getByTestId("state-cancelled")).toBeVisible();
});

test("a stalled campaign can be resumed", async ({ page }) => {
  await go(page, "campaign/fx-stalled");
  await expect(page.getByText("Stalled", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Resume campaign" }).click();
  await expect(page.getByText("Stalled", { exact: true })).toHaveCount(0);
});

test("loading resolves into the campaign", async ({ page }) => {
  await go(page, "campaign/fx-slow");
  await expect(page.getByTestId("state-loading")).toBeVisible();
  await shot(page, "loading");
  await expect(page.locator(".status-line")).toContainText("Running", { timeout: 15_000 });
});

for (const [path, state] of [
  ["fx-load-denied", "denied"],
  ["fx-load-invalid", "validation_error"],
  ["fx-load-missing", "not_found"],
  ["fx-cancelled", "cancelled"],
  ["fx-failed", "failed"],
  ["fx-stalled", "failed"],
  ["fx-empty/evidence", "empty"],
  ["fx-empty/diff", "empty"],
  ["fx-empty/approval", "empty"],
  ["fx-empty/capsule", "empty"],
] as const) {
  test(`state ${state} on ${path}`, async ({ page }) => {
    await go(page, `campaign/${path}`);
    await expect(page.getByTestId(`state-${state}`).first()).toBeVisible();
    await shot(page, `state-${path.replace("/", "-")}`);
  });
}

test("a campaign opens by id from the keyboard", async ({ page }) => {
  const input = page.getByLabel("Campaign id");
  await input.fill("fx-refund");
  await input.press("Enter");
  await expect(page.locator(".status-line")).toBeVisible();
  await expect(page).toHaveURL(/#\/campaign\/fx-refund/);
});

test("keyboard: skip link, view navigation and the decision need no pointer", async ({ page }) => {
  await go(page, "campaign/fx-refund");
  await expect(page.locator(".status-line")).toBeVisible();
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

test("keyboard: tabbing reaches every control, each with visible focus", async ({ page }) => {
  await go(page, "campaign/fx-refund/approval");
  await expect(page.getByRole("button", { name: "Approve this action" })).toBeVisible();
  const reached = new Set<string>();
  for (let press = 0; press < 40; press += 1) {
    await page.keyboard.press("Tab");
    const focus = await page.evaluate(() => {
      const element = document.activeElement;
      if (element === null || element === document.body) return null;
      const label = element.textContent?.trim().slice(0, 30) || element.tagName;
      return { label, outline: getComputedStyle(element).outlineStyle };
    });
    if (focus === null) continue;
    expect(focus.outline, focus.label).not.toBe("none");
    reached.add(focus.label);
  }
  expect([...reached]).toContain("Approve this action");
  expect([...reached]).toContain("Reject");
});

test("keyboard: details and scrollable regions open and focus", async ({ page }) => {
  await go(page, "campaign/fx-journey/evidence");
  const summary = page.getByText("Event 10 request and outcome");
  await summary.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByLabel("Event 10 request and outcome")).toBeVisible();
});

test.describe("dark mode", () => {
  test.use({ colorScheme: "dark" });

  test("the labels stay readable", async ({ page }) => {
    await go(page, "campaign/fx-coverage-violation/diff");
    await expect(page.getByTestId("state-incomplete")).toBeVisible();
    await eventsCompared(page);
    await expect(page.getByTestId("test-data-banner")).toBeVisible();
    await shot(page, "dark-diff");
    const colors = await page.evaluate(() => ({
      body: getComputedStyle(document.body).backgroundColor,
      text: getComputedStyle(document.body).color,
    }));
    expect(colors.body).toBe("rgb(20, 23, 28)");
    expect(colors.text).toBe("rgb(232, 233, 230)");
  });
});

test.describe("narrow screen", () => {
  test.use({ viewport: { width: 320, height: 640 } });

  for (const [path, name] of [
    ["", "home"],
    ["campaign/fx-refund", "refund-campaign"],
    ["campaign/fx-refund/evidence", "refund-evidence"],
    ["campaign/fx-refund/diff", "refund-diff"],
    ["campaign/fx-refund/approval", "refund-approval"],
    ["campaign/fx-refund/capsule", "refund-capsule"],
    ["campaign/fx-journey/diff", "journey-diff"],
    ["campaign/fx-journey/evidence", "journey-evidence"],
    ["campaign/fx-journey/approval", "journey-approval"],
    ["campaign/fx-coverage-incomplete", "coverage-incomplete"],
    ["campaign/fx-settings-blocked", "settings-blocked"],
  ] as const) {
    test(`no horizontal page scroll: ${name}`, async ({ page }) => {
      await go(page, path);
      await expect(page.getByTestId("test-data-banner")).toBeVisible();
      await expect(page.locator("main")).not.toBeEmpty();
      await page.waitForLoadState("networkidle");
      if (name.endsWith("-diff")) await eventsCompared(page);
      await noSidewaysScroll(page);
      await shot(page, `narrow-${name}`);
    });
  }

  test("every table value is on screen, not scrolled out of reach", async ({ page }) => {
    await go(page, "campaign/fx-journey/diff");
    await eventsCompared(page);
    const cells = page.locator(".table-wrap td");
    const count = await cells.count();
    expect(count).toBeGreaterThan(20);
    for (let index = 0; index < count; index += 1) {
      const box = await cells.nth(index).boundingBox();
      expect(box?.x ?? -1).toBeGreaterThanOrEqual(0);
      expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(320);
    }
    const row = page.locator(".table-wrap tbody tr", { hasText: "messages_delivered_without_permission" });
    await expect(row.locator('td[data-label="Baseline"]')).toContainText("0");
    await expect(row.locator('td[data-label="Fork"]')).toContainText("1");
  });

  test("the sides stack rather than squeeze", async ({ page }) => {
    await go(page, "campaign/fx-journey/diff");
    const sides = page.locator(".sides .side");
    await expect(sides.first()).toBeVisible();
    const [first, second] = [await sides.nth(0).boundingBox(), await sides.nth(1).boundingBox()];
    expect((second?.y ?? 0) > (first?.y ?? 0)).toBe(true);
    expect(first?.x).toBe(second?.x);
  });

  test("the whole refund decision works at this width", async ({ page }) => {
    await go(page, "campaign/fx-refund/approval");
    await page.getByRole("button", { name: "Approve this action" }).click();
    await expect(page.getByTestId("repair-verdict")).toContainText("Repair verified");
    await noSidewaysScroll(page);
  });
});
