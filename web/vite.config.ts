import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const path = (relative: string) => fileURLToPath(new URL(relative, import.meta.url));

/**
 * The HTML entry lives in src/ because M08 owns web/src, not web/. Vite compiles TSX
 * without a plugin, using the automatic JSX runtime configured here.
 *
 * `--mode fixture` builds the test-data interface. Any other mode talks to M07 and the
 * fixture code is dropped from the bundle.
 */
export default defineConfig({
  plugins: [
    {
      name: "production-csp",
      // Only the dev server needs its HMR socket, so a build ships without it.
      transformIndexHtml: (html, context) => (context.server ? html : html.replace(/ ws:\/\/[^\s;]+/g, "")),
    },
  ],
  root: path("./src"),
  publicDir: path("./public"),
  oxc: { jsx: { runtime: "automatic" } },
  build: { outDir: path("./dist"), emptyOutDir: true, target: "es2022" },
  server: {
    fs: { allow: [path("..")] },
    proxy: {
      // M07 serves loopback only and checks Host, so the proxied Host stays local.
      "/v1": { target: process.env.GHOSTLAB_API_ORIGIN ?? "http://127.0.0.1:8787", changeOrigin: false },
    },
  },
});
