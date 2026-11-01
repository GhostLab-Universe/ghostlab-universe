/** No intercepted routes or fixture UI: these act on the candidate's real API records. */
import { test, expect } from "@playwright/test";
import { readFile, readdir } from "node:fs/promises";
import { resolve, join } from "node:path";
import { randomUUID } from "node:crypto";

for (const world of ["refund-world/1", "group-journey/1"]) {
  test(`stored evidence, bound approval, repair and capsule: ${world}`, async ({ page, request }) => {
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto("/#/");
    await expect(page.locator(".session")).toContainText("inference scripted");
    await page.getByLabel("World").selectOption(world);
    await page.getByLabel(/Scripted scenario/).selectOption(world === "group-journey/1" ? "revocation_race" : "default");
    await page.getByLabel("Objective").fill(`Application acceptance ${randomUUID()}`);
    await page.getByLabel("Success criterion").fill("The same failure and enforced repair reproduce");
    await page.getByRole("button", { name: "Start campaign" }).click();
    await expect(page.getByTestId("state-awaiting_approval")).toBeVisible();
    const campaign = /#\/campaign\/(c-[^/]+)/.exec(page.url())?.[1];
    expect(campaign).toBeTruthy();
    const view = await (await request.get(`/v1/campaigns/${campaign}`)).json();
    expect(view.inference).toBe("scripted");
    const experiment = view.experiments[0].experiment_id;
    const base = `/v1/campaigns/${campaign}/experiments/${experiment}`;
    const record = await (await request.get(`${base}/diff`)).json();
    expect(record.coverage.complete).toBe(true);
    const nav = page.getByRole("navigation", { name: "Campaign views" });
    await nav.getByRole("link", { name: "Reality Diff" }).click();
    await expect(page.locator("table.aligned")).toBeVisible();
    if (world === "group-journey/1") {
      await expect(page.getByTestId("leak-story")).toContainText("event 18");
      expect(record.diff.violated_invariants).toContain("delivered_messages_permitted");
    } else {
      expect(record.diff.first_divergence.event_id).toBe(2);
      await expect(page.getByText("Total refunded 500000 minor units")).toBeVisible();
    }
    await nav.getByRole("link", { name: /Approval/ }).click();
    await page.getByRole("button", { name: "Approve this action" }).click();
    await expect(page.getByTestId("repair-verdict")).toContainText("Repair verified");
    const completed = await (await request.get(`/v1/campaigns/${campaign}`)).json();
    expect(completed.outcome).toBe("repair_verified");
    expect(completed.repair.verified).toBe(true);
    if (world === "group-journey/1") expect(completed.repair.held_by).toBe("guarded_boundary");
    const calls = completed.record.usage.model_calls;
    await nav.getByRole("link", { name: "Capsule" }).click();
    await page.getByLabel("Run").selectOption("repaired");
    await page.getByRole("button", { name: "Export capsule" }).click();
    await page.getByRole("button", { name: "Replay capsule" }).click();
    await expect(page.getByTestId("replay-verdict")).toContainText("Reproduced");
    await expect(page.getByTestId("replay-fields")).toContainText(/model_calls\s*0/);
    expect((await (await request.get(`/v1/campaigns/${campaign}`)).json()).record.usage.model_calls).toBe(calls);
    expect(errors).toEqual([]);
  });
}

test("the browser receives no configured provider canary in payloads or production assets", async ({ page }) => {
  const canary = "synthetic-provider-canary-9123456789";
  const leaks: string[] = [];
  const reads: Promise<void>[] = [];
  let inspectedResponses = 0;
  page.on("request", request => {
    if (JSON.stringify(request.headers()).includes(canary) || (request.postData() ?? "").includes(canary)) leaks.push("request");
  });
  page.on("response", response => {
    reads.push((async () => {
      if (response.url().startsWith("http://127.0.0.1:")) {
        const text = await response.text();
        inspectedResponses += 1;
        if (text.includes(canary)) leaks.push(response.url());
      }
    })());
  });
  await page.goto("/#/");
  await expect(page.locator(".session")).toContainText("inference scripted");
  await Promise.all(reads);
  expect(inspectedResponses).toBeGreaterThan(2);
  const web = resolve(import.meta.dirname, "../..");
  const out = resolve(process.env.GHOSTLAB_APPLICATION_OUT ?? join(web, "node_modules/.application-acceptance"));
  async function scan(directory: string): Promise<void> {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) await scan(path);
      else expect((await readFile(path)).includes(Buffer.from(canary))).toBe(false);
    }
  }
  await scan(join(out, "dist"));
  expect(leaks).toEqual([]);
});
