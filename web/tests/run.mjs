// Runs the M08 tests that need no browser. From web/: `node tests/run.mjs`.
// Unit tests run directly (Node strips the types); render tests are bundled first.
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const web = fileURLToPath(new URL("..", import.meta.url));
const out = process.env.M08_RENDER_OUT ?? fileURLToPath(new URL("../node_modules/.m08-render/", import.meta.url));

function run(label, command, args) {
  console.log(`\n== ${label}`);
  const result = spawnSync(command, args, { cwd: web, stdio: "inherit", env: { ...process.env, M08_RENDER_OUT: out } });
  if (result.status !== 0) process.exit(result.status ?? 1);
}

run("unit", process.execPath, ["--test", "tests/unit/*.test.ts"]);
run("render bundle", process.execPath, [
  "node_modules/vite/bin/vite.js",
  "build",
  "--config",
  "tests/vite.render.config.ts",
]);
run("render", process.execPath, ["--test", `${out.replaceAll("\\", "/")}/panels.test.js`]);
