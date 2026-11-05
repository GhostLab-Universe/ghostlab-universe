import { defineConfig } from "vite";

// Track D guided UI. No frontend plugins: the app uses React.createElement in
// plain .ts (no JSX transform needed), so no new dependencies are required.
// A request for @vitejs/plugin-react + @types/react goes to M00 separately.
declare const process: { env: Record<string, string | undefined> }; // ponytail: no @types/node for one env read
// GHOSTLAB_API points a second checkout at its own service port (e.g. 8918).
const API = process.env["GHOSTLAB_API"] ?? "http://127.0.0.1:8917";

export default defineConfig({
  build: {
    outDir: "dist",
    sourcemap: false,
  },
  server: {
    // Approved M00 fixtures live outside web/ and are inlined at build time.
    fs: { allow: [".."] },
    // Same-origin /v1 for local recording; the service is uvicorn on 8917.
    proxy: { "/v1": API },
  },
  preview: { proxy: { "/v1": API } },
});
