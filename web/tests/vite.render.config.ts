import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const path = (relative: string) => fileURLToPath(new URL(relative, import.meta.url));

/**
 * Bundles the TSX render tests for Node, because Node strips TypeScript but not JSX.
 * Output goes to M08_RENDER_OUT, or node_modules/.m08-render, and is run by node:test.
 */
export default defineConfig({
  root: path("../src"),
  publicDir: false,
  logLevel: "warn",
  oxc: { jsx: { runtime: "automatic" } },
  build: {
    ssr: path("./render/panels.test.tsx"),
    outDir: process.env.M08_RENDER_OUT ?? path("../node_modules/.m08-render"),
    emptyOutDir: true,
    minify: false,
    target: "node22",
  },
});
