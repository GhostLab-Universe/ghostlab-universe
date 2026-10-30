import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const path = (relative: string) => fileURLToPath(new URL(relative, import.meta.url));

/**
 * Bundles the TSX render tests for Node, because Node strips TypeScript but not JSX.
 * Output goes to M08_RENDER_OUT, or node_modules/.m08-render, and is run by node:test.
 * M08_RENDER_ENTRY picks the test files, comma separated, relative to web/tests.
 */
export default defineConfig({
  root: path("../src"),
  publicDir: false,
  logLevel: "warn",
  oxc: { jsx: { runtime: "automatic" } },
  build: {
    rollupOptions: {
      input: (process.env.M08_RENDER_ENTRY ?? "render/panels.test.tsx,render/outcomes.test.tsx")
        .split(",")
        .map((entry) => path(`./${entry}`)),
    },
    ssr: true,
    outDir: process.env.M08_RENDER_OUT ?? path("../node_modules/.m08-render"),
    emptyOutDir: true,
    minify: false,
    target: "node22",
  },
});
