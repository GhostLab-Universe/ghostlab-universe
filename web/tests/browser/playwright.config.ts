// NOT YET RUN: see interface.spec.ts. Needs @playwright/test from M00.
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  use: { baseURL: "http://127.0.0.1:5173" },
  webServer: {
    command: "node ../../node_modules/vite/bin/vite.js --config ../../vite.config.ts --mode fixture --host 127.0.0.1 --port 5173 --strictPort",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: false,
  },
});
