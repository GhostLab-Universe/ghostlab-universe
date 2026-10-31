/**
 * Headless Chrome against a production build served by `vite preview`, so the CSP and
 * bundling are what ships. M08_BROWSER_MODE picks the run:
 *
 *   fixture (default)  the --mode fixture build, over labelled test data
 *   service            the API build, proxied to GHOSTLAB_API_ORIGIN. Start
 *                      tests/integration/outcome_server.py first. Inference is scripted.
 *   scripted           the API build against `python -m ghostlab.api --scripted`, for the
 *                      demo. Proposals are checked-in replies, never a model.
 *
 * M08_BROWSER_SERVER=dev uses the dev server instead of a production build, to check
 * `npm run dev` under the same CSP.
 *
 * M08_BROWSER_OUT holds builds, results and screenshots. Point TEMP there too, to keep
 * Chrome's profile out of the system temp folder.
 */
import { join, resolve } from "node:path";
import { defineConfig } from "@playwright/test";

const named = process.env.M08_BROWSER_MODE;
const mode = named === "service" || named === "scripted" ? named : "fixture";
const out = resolve(process.env.M08_BROWSER_OUT ?? "node_modules/.m08-browser", mode);
const ports = { fixture: 5181, service: 5182, scripted: 5184 };
const port = Number(process.env.M08_BROWSER_PORT ?? ports[mode]);
const vite = "node node_modules/vite/bin/vite.js";
const dist = join(out, "dist");
const modeFlag = mode === "fixture" ? "--mode fixture " : "";
const build = `${vite} build ${modeFlag}--outDir "${dist}" --emptyOutDir`;
const preview = `${vite} preview --outDir "${dist}" --host 127.0.0.1 --port ${port} --strictPort`;
const dev = `${vite} ${modeFlag}--host 127.0.0.1 --port ${port} --strictPort`;

export default defineConfig({
  testDir: ".",
  testMatch: { fixture: "interface.spec.ts", service: "service.spec.ts", scripted: "scripted.spec.ts" }[mode],
  outputDir: join(out, "results"),
  workers: mode === "fixture" ? 4 : 2,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"], ["json", { outputFile: join(out, "results.json") }]],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    channel: "chrome",
    headless: true,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: {
    command: process.env.M08_BROWSER_SERVER === "dev" ? dev : `${build} && ${preview}`,
    cwd: resolve(import.meta.dirname, "../.."),
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
