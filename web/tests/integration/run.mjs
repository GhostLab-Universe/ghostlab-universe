// Runs the M07 integration tests. From web/, with a service running:
//   M07_ORIGIN=http://127.0.0.1:8788 node tests/integration/run.mjs
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

if (!process.env.M07_ORIGIN) {
  console.error("Set M07_ORIGIN to a running M07 service. See m07-flow.test.tsx.");
  process.exit(2);
}
const web = fileURLToPath(new URL("../..", import.meta.url));
const out = fileURLToPath(new URL("../../node_modules/.m08-integration/", import.meta.url));
const env = { ...process.env, M08_RENDER_OUT: out, M08_RENDER_ENTRY: "integration/m07-flow.test.tsx" };

function run(args) {
  const result = spawnSync(process.execPath, args, { cwd: web, stdio: "inherit", env });
  if (result.status !== 0) process.exit(result.status ?? 1);
}

run(["node_modules/vite/bin/vite.js", "build", "--config", "tests/vite.render.config.ts"]);
run(["--test", `${out.replaceAll("\\", "/")}/m07-flow.test.js`]);
