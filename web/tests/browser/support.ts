import { join, resolve } from "node:path";
import { expect, test as base, type Page } from "@playwright/test";

const named = process.env.M08_BROWSER_MODE;
const mode = named === "service" || named === "scripted" ? named : "fixture";
const out = resolve(process.env.M08_BROWSER_OUT ?? "node_modules/.m08-browser", mode, "screenshots");

/** Fails any test whose page threw, broke the CSP, or logged a React error. */
export const test = base.extend<{ problems: string[] }>({
  problems: [
    async ({ page }, use) => {
      const problems: string[] = [];
      page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));
      page.on("console", (message) => {
        const text = message.text();
        // The browser logs each 4xx. Those are expected here and asserted by the tests.
        if (message.type() === "error" && !text.startsWith("Failed to load resource")) {
          problems.push(`console: ${text}`);
        }
      });
      await use(problems);
      expect(problems).toEqual([]);
    },
    { auto: true },
  ],
});

export { expect };

export const go = (page: Page, path: string) => page.goto(`/#/${path}`);

export const view = (page: Page, name: string | RegExp) =>
  page.getByRole("navigation", { name: "Campaign views" }).getByRole("link", { name }).click();

/** Fails rather than hides content if the page ever scrolls sideways. */
export async function noSidewaysScroll(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

/** The aligned table only exists once both runs have loaded. */
export const eventsCompared = (page: Page) => expect(page.locator("table.aligned")).toBeVisible();

export async function shot(page: Page, name: string) {
  await page.screenshot({ path: join(out, `${name}.png`), fullPage: true });
}
