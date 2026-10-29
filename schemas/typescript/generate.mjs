// Generates schemas/typescript/contracts.ts from the canonical JSON Schema bundle.
// Run from web/: `npm run contracts:generate` (or `contracts:verify` to check drift).
import { readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

// Resolve the pinned generator from web/ (the npm package that locks it).
const web = createRequire(join(process.cwd(), "package.json"));
const { compile } = await import(pathToFileURL(web.resolve("json-schema-to-typescript")).href);

const here = new URL(".", import.meta.url);
const bundle = JSON.parse(await readFile(new URL("../v1/contracts.bundle.json", here), "utf8"));
const banner = "/* Generated from schemas/v1/contracts.bundle.json by schemas/typescript/generate.mjs. Do not edit. */";
const output = await compile(bundle, "GhostLabContracts", {
  bannerComment: banner,
  additionalProperties: false,
  strictIndexSignatures: true,
  unreachableDefinitions: true,
  format: true,
});
const target = new URL("contracts.ts", here);
if (process.argv.includes("--check")) {
  const current = (await readFile(target, "utf8").catch(() => "")).replace(/\r\n/g, "\n");
  if (current !== output) {
    console.error("drift: schemas/typescript/contracts.ts");
    process.exit(1);
  }
  console.log("contracts.ts matches contracts.bundle.json");
} else {
  await writeFile(target, output);
  console.log("wrote schemas/typescript/contracts.ts");
}
