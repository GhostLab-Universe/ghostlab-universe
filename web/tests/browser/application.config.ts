/** Candidate application acceptance: real CLI/API and production UI, scripted inference. */
import { resolve, join } from "node:path";
import { defineConfig } from "@playwright/test";

const web = resolve(import.meta.dirname, "../..");
const root = resolve(web, "..");
const out = resolve(process.env.GHOSTLAB_APPLICATION_OUT ?? join(web, "node_modules/.application-acceptance"));
const api = Number(process.env.GHOSTLAB_APPLICATION_API_PORT ?? 18787);
const ui = Number(process.env.GHOSTLAB_APPLICATION_UI_PORT ?? 15184);
const python = process.env.GHOSTLAB_ACCEPTANCE_PYTHON ?? "python";
const canary = "synthetic-provider-canary-9123456789";
const channel = process.env.GHOSTLAB_BROWSER_CHANNEL;
const dist = join(out, "dist");
const vite = "node node_modules/vite/bin/vite.js";

export default defineConfig({
  testDir: ".", testMatch: "application.spec.ts", workers: 1, timeout: 60000,
  outputDir: join(out, "results"),
  reporter: [["list"], ["json", { outputFile: join(out, "results.json") }]],
  expect: { timeout: 30000 },
  use: { baseURL: `http://127.0.0.1:${ui}`, headless: true,
         ...(channel ? { channel } : {}), screenshot: "only-on-failure", trace: "retain-on-failure" },
  webServer: [
    { command: `"${python}" -m ghostlab.api --scripted --port ${api} --root "${join(out, "store")}"`,
      cwd: root, url: `http://127.0.0.1:${api}/v1/session`, reuseExistingServer: false, timeout: 60000,
      env: { PYTHONPATH: join(root, "src"), GHOSTLAB_AUTH_MODE: "local_dev",
             MODEL_API_KEY: canary, BASETEN_API_KEY: canary, LANGSMITH_TRACING: "false" } },
    { command: `${vite} build --outDir "${dist}" --emptyOutDir && ${vite} preview --outDir "${dist}" --host 127.0.0.1 --port ${ui} --strictPort`,
      cwd: web, url: `http://127.0.0.1:${ui}`, reuseExistingServer: false, timeout: 60000,
      env: { GHOSTLAB_API_ORIGIN: `http://127.0.0.1:${api}` } },
  ],
});
