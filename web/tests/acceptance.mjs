#!/usr/bin/env node
// Guided-UI production acceptance: serves web/dist over 127.0.0.1, drives the
// real guided routes in headless Chrome via CDP, and verifies the 7-step flow
// end to end. Stdlib only (fetch + WebSocket + CDP); no new dependencies.
// State stays in a track-private os.tmpdir() directory; nothing is written
// to the repo.
//
//   node web/tests/acceptance.mjs [--dist=web/dist] [--chrome=PATH]
//     [--service=http://127.0.0.1:PORT] [--allow-static] [--keep-temp]
//
// Without --service the static server refuses /v1 (dead-service simulation)
// and the suite asserts connection-error states with the config preserved —
// fixtures must never appear. With --service the /v1 paths proxy to a live
// E4 service and the suite asserts real execution end to end (references,
// provenance, export, compare, retest, reload recovery).
//
// Exit 0: all checks passed. Exit 1: a check failed. Exit 2: environment
// cannot run the browser-driven checks (see --allow-static).

import { spawn, execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { promises as fs } from "node:fs";
import { createServer } from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "..", "..");
const webRoot = path.resolve(here, "..");

const args = Object.fromEntries(
  process.argv.slice(2).map((a) => {
    const m = /^--([^=]+)(=(.*))?$/.exec(a);
    return m ? [m[1], m[3] ?? "true"] : ["_pos", a];
  }),
);
const distDir = path.resolve(args.dist ?? path.join(webRoot, "dist"));
const allowStatic = args["allow-static"] === "true";
const serviceBase = typeof args.service === "string" && args.service !== "true" ? args.service.replace(/\/+$/, "") : null;

const STEP_ROUTES = [
  "/guide/1-environment",
  "/guide/2-qualification",
  "/guide/3-agent",
  "/guide/4-configure",
  "/guide/5-review",
  "/guide/6-run",
  "/guide/7-export",
];

const SECRET_PATTERNS = [
  /model_api_key\s*[:=]\s*['"][^'"]{8,}/i,
  /baseten_api_key\s*[:=]\s*['"][^'"]{8,}/i,
  /api[_-]?key\s*[:=]\s*['"][^'"]{8,}['"]/i,
  /begin [a-z ]*private key/i,
  /bearer\s+[A-Za-z0-9\-._~+/]{12,}={0,2}/i,
];

const BUNDLE_MARKERS = [
  "/guide/1-environment",
  "/guide/7-export",
  "__ghostlab",
  "recorded-config",
  "run-references",
  "service-error",
  "fixture-refused",
  "mapping-notes",
  "arms-table",
  "export-fetch",
];

// Production builds must not instantiate the fixture path at all: the
// dev-only import is dead code under `vite build`, so these strings — the
// fixture class, the fixture mode value and the scripted-run copy — must be
// absent from every emitted chunk.
const BUNDLE_FORBIDDEN = [
  "FixtureExperimentApi",
  "fixture-scripted",
  "Scripted run",
];

const checks = [];
function check(name, ok, detail = "") {
  checks.push({ name, ok: Boolean(ok), detail: String(detail ?? "") });
  if (!ok) console.error(`FAIL ${name}${detail ? `: ${detail}` : ""}`);
  else console.log(`ok ${name}`);
}

async function walkFiles(dir) {
  const out = [];
  for (const entry of await fs.readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await walkFiles(full)));
    else out.push(full);
  }
  return out;
}

async function scanForSecrets() {
  const files = [...(await walkFiles(distDir)), ...(await walkFiles(path.join(webRoot, "src")))];
  let hits = 0;
  for (const file of files) {
    let text = "";
    try {
      text = await fs.readFile(file, "utf-8");
    } catch {
      continue;
    }
    for (const pattern of SECRET_PATTERNS) {
      if (pattern.test(text)) {
        hits += 1;
        console.error(`secret pattern ${pattern} hit in ${path.relative(repoRoot, file)}`);
      }
    }
  }
  check("no-secret-patterns", hits === 0, hits === 0 ? `${files.length} files scanned` : `${hits} hits`);
}

async function scanBundleMarkers() {
  const files = await walkFiles(distDir);
  const bundle = (
    await Promise.all(files.filter((f) => f.endsWith(".js")).map((f) => fs.readFile(f, "utf-8")))
  ).join("\n");
  const missing = BUNDLE_MARKERS.filter((m) => !bundle.includes(m));
  check("bundle-markers", missing.length === 0, missing.length === 0 ? "all markers present" : `missing: ${missing.join(", ")}`);
  const leaked = BUNDLE_FORBIDDEN.filter((m) => bundle.includes(m));
  check("bundle-no-fixture", leaked.length === 0, leaked.length === 0 ? "fixture path absent from prod bundle" : `leaked: ${leaked.join(", ")}`);
}

const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".json": "application/json", ".txt": "text/plain" };

function startServer() {
  return new Promise((resolve) => {
    const server = createServer(async (req, res) => {
      try {
        const url = new URL(req.url ?? "/", "http://127.0.0.1");
        if (url.pathname === "/v1" || url.pathname.startsWith("/v1/")) {
          if (serviceBase === null) {
            // Dead-service simulation: refuse the connection so the page
            // observes a real network failure (never a synthesized error).
            req.socket.destroy();
            return;
          }
          const chunks = [];
          for await (const chunk of req) chunks.push(chunk);
          const forward = await fetch(serviceBase + url.pathname + url.search, {
            method: req.method,
            headers: {
              "content-type": req.headers["content-type"] ?? "application/json",
              "x-ghostlab-project": req.headers["x-ghostlab-project"] ?? "",
            },
            body: ["GET", "HEAD"].includes(req.method ?? "GET") ? undefined : Buffer.concat(chunks),
          });
          const body = Buffer.from(await forward.arrayBuffer());
          res.writeHead(forward.status, { "content-type": forward.headers.get("content-type") ?? "application/json" });
          res.end(body);
          return;
        }
        let target = path.normalize(path.join(distDir, decodeURIComponent(url.pathname)));
        if (!target.startsWith(distDir)) {
          res.writeHead(403);
          res.end("forbidden");
          return;
        }
        try {
          const stat = await fs.stat(target);
          if (stat.isDirectory()) target = path.join(target, "index.html");
        } catch {
          if (path.extname(target) === "") target = path.join(distDir, "index.html");
          else {
            res.writeHead(404);
            res.end("not found");
            return;
          }
        }
        const body = await fs.readFile(target);
        res.writeHead(200, { "content-type": MIME[path.extname(target)] ?? "application/octet-stream" });
        res.end(body);
      } catch {
        try {
          res.writeHead(500);
          res.end("error");
        } catch { /* socket gone */ }
      }
    });
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      resolve({ server, base: `http://127.0.0.1:${address.port}` });
    });
  });
}

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.on("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const port = probe.address().port;
      probe.close(() => resolve(port));
    });
  });
}

async function findChromeAsync() {
  const direct = args.chrome ?? process.env.CHROME_PATH;
  if (direct) return direct;
  for (const p of [
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
  ]) {
    try {
      await fs.stat(p);
      return p;
    } catch {
      /* next */
    }
  }
  return null;
}

function connectCDP(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    const pending = new Map();
    const handlers = new Map();
    let nextId = 1;
    ws.onopen = () =>
      resolve({
        ws,
        on: (method, fn) => {
          const list = handlers.get(method) ?? [];
          list.push(fn);
          handlers.set(method, list);
        },
        send: (method, params = {}) =>
          new Promise((res, rej) => {
            const id = nextId++;
            pending.set(id, { res, rej });
            ws.send(JSON.stringify({ id, method, params }));
            setTimeout(() => {
              if (pending.has(id)) {
                pending.delete(id);
                rej(new Error(`CDP timeout: ${method}`));
              }
            }, 20000);
          }),
      });
    ws.onerror = () => reject(new Error("CDP websocket error"));
    ws.onmessage = (event) => {
      const msg = JSON.parse(String(event.data));
      if (msg.id !== undefined && pending.has(msg.id)) {
        const { res, rej } = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) rej(new Error(`CDP ${JSON.stringify(msg.error)}`));
        else res(msg.result);
      } else if (msg.method && handlers.has(msg.method)) {
        for (const fn of handlers.get(msg.method)) fn(msg.params);
      }
    };
  });
}

const qText = (testid) =>
  `(function(){const el=document.querySelector('[data-testid="${testid}"]');return el?el.textContent:'';})()`;
const qExists = (testid) => `document.querySelector('[data-testid="${testid}"]')!==null`;
const clickJS = (testid) =>
  `(function(){const el=document.querySelector('[data-testid="${testid}"]');if(!el) return 'missing';el.click();return 'ok';})()`;
const setInputJS = (testid, value) =>
  `(function(){const el=document.querySelector('[data-testid="${testid}"]');if(!el) return 'missing';` +
  `const proto=el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;` +
  `Object.getOwnPropertyDescriptor(proto,'value').set.call(el,${JSON.stringify(value)});` +
  `el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));return 'ok';})()`;

async function staticOnly(base) {
  for (const route of ["/", ...STEP_ROUTES]) {
    const res = await fetch(base + route);
    const text = await res.text();
    check(`static-route ${route}`, res.status === 200 && text.includes('id="root"'), `status ${res.status}`);
  }
}

function runReplay(capsulePath) {
  const bins = process.platform === "win32" ? [["python", []], ["py", ["-3"]]] : [["python3", []], ["python", []]];
  return (async () => {
    let last = "no python tried";
    for (const [bin, prefix] of bins) {
      try {
        return await new Promise((resolve, reject) => {
          execFile(
            bin,
            [...prefix, "-m", "ghostlab", "replay", capsulePath],
            { cwd: repoRoot, env: { ...process.env, PYTHONPATH: path.join(repoRoot, "src") }, timeout: 60000 },
            (error, stdout, stderr) => (error ? reject(new Error(stderr || error.message)) : resolve(stdout)),
          );
        });
      } catch (error) {
        last = `${bin}: ${error.message}`;
      }
    }
    throw new Error(`python replay unavailable (${last})`);
  })();
}

async function main() {
  try {
    await fs.stat(path.join(distDir, "index.html"));
  } catch {
    console.error(`dist not found at ${distDir}; build first (see docs/modules/M08.md).`);
    process.exit(2);
  }

  await scanForSecrets();
  await scanBundleMarkers();

  const { server, base } = await startServer();
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), "track-d-accept-"));
  const shots = path.join(temp, "shots");
  await fs.mkdir(shots, { recursive: true });
  console.log(`base ${base}`);
  console.log(`temp ${temp}`);
  console.log(`service ${serviceBase ?? "(dead-service simulation)"}`);

  // The committed capsule replays through the real replay tooling in both
  // modes (replay tooling is independent of the UI path under test).
  try {
    const replayOut = await runReplay(path.join(repoRoot, "examples", "contracts", "core", "capsule_v1.refund_failure.json"));
    const replay = JSON.parse(replayOut);
    check("committed-capsule-replays", replay?.reproduced === true && replay?.model_calls === 0, replayOut.trim().slice(0, 120));
  } catch (error) {
    check("committed-capsule-replays", false, error instanceof Error ? error.message : String(error));
  }

  const chrome = await findChromeAsync();
  if (!chrome) {
    if (!allowStatic) {
      console.error("no headless Chrome/Edge found; rerun with --allow-static for the static-only gate.");
      server.close();
      process.exit(2);
    }
    console.warn("BROWSER-DRIVEN CHECKS SKIPPED: no browser binary; static-only mode.");
    await staticOnly(base);
    server.close();
    const failed = checks.filter((c) => !c.ok);
    console.log(JSON.stringify({ mode: "static-only", failed: failed.length, checks }, null, 2));
    process.exit(failed.length === 0 ? 0 : 1);
  }

  const debugPort = await freePort();
  const profile = path.join(temp, "profile");
  const child = spawn(chrome, [
    "--headless=new",
    "--disable-gpu",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    `--remote-debugging-port=${debugPort}`,
    `--user-data-dir=${profile}`,
    "about:blank",
  ], { stdio: "ignore" });

  const closeAll = () => {
    try { child.kill(); } catch { /* gone */ }
    server.close();
  };

  try {
    let debuggerUrl = null;
    for (let i = 0; i < 100 && !debuggerUrl; i++) {
      await new Promise((r) => setTimeout(r, 200));
      try {
        const res = await fetch(`http://127.0.0.1:${debugPort}/json/list`);
        const targets = await res.json();
        const page = targets.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
        if (page) debuggerUrl = page.webSocketDebuggerUrl;
      } catch { /* not up yet */ }
    }
    if (!debuggerUrl) throw new Error("remote debugging endpoint never appeared");
    check("browser-launch", true, chrome);

    const cdp = await connectCDP(debuggerUrl);
    const consoleErrors = [];
    const pageErrors = [];
    cdp.on("Runtime.exceptionThrown", (p) => pageErrors.push(JSON.stringify(p?.exceptionDetails?.text ?? p).slice(0, 300)));
    cdp.on("Log.entryAdded", (p) => {
      if (p?.entry?.level !== "error") return;
      const text = String(p.entry.text);
      // "Failed to load resource" entries are the suite's own asserted error
      // paths surfacing in the console, not app breakage: dead-service mode
      // refuses /v1 connections on purpose, and live-service mode asserts an
      // unknown-manifest 404, a missing-report 404 and a dead-override
      // connection failure. Each is asserted via its UI state; uncaught app
      // exceptions stay strict under no-page-exceptions.
      if (/^Failed to load resource: /.test(text)) return;
      consoleErrors.push(text.slice(0, 300));
    });
    await cdp.send("Log.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Page.enable");

    const evaluate = async (expression) => {
      const result = await cdp.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      return result?.result?.value;
    };
    const waitFor = async (expression, label, timeoutMs = 20000) => {
      const start = Date.now();
      for (;;) {
        if (await evaluate(expression)) return true;
        if (Date.now() - start > timeoutMs) throw new Error(`timed out waiting for ${label}`);
        await new Promise((r) => setTimeout(r, 250));
      }
    };
    const shot = async (name) => {
      const { data } = await cdp.send("Page.captureScreenshot", { format: "png" });
      await fs.writeFile(path.join(shots, `${name}.png`), Buffer.from(data, "base64"));
    };
    const pageLoad = async (route, marker) => {
      await cdp.send("Page.navigate", { url: base + route });
      await waitFor(marker, marker);
    };
    // In-app navigation through the stepper: full page loads would reset the
    // guide's in-memory config, so the flow below drives the SPA like a user.
    const gotoViaApp = async (route, marker) => {
      const clicked = await evaluate(
        `(function(){const a=document.querySelector('a[href="${route}"]');if(!a) return 'missing';a.click();return 'ok';})()`,
      );
      if (clicked !== "ok") throw new Error(`missing in-app link to ${route}`);
      await waitFor(marker, marker);
      const pathname = await evaluate("window.location.pathname");
      if (pathname !== route) throw new Error(`expected route ${route}, at ${pathname}`);
    };

    if (serviceBase === null) await deadServiceFlow({ cdp, evaluate, waitFor, shot, pageLoad, gotoViaApp });
    else await liveServiceFlow({ cdp, evaluate, waitFor, shot, pageLoad, gotoViaApp });

    // Full-load routes: deep links serve the app shell; fresh loads reset to
    // honest defaults; unknown routes render NotFound instead of breaking.
    // (Recovery may restore a stored run on this fresh load; defaults are
    // asserted in a clean query that skips recovery-dependent state.)
    await pageLoad("/guide/99-nope", `document.body.innerText.includes('Unknown guided-run route')`);
    check("unknown-route", await evaluate(`document.body.innerText.includes('Unknown guided-run route')`));
    await pageLoad("/guide/1-environment?fixture=1", qExists("fixture-refused"));
    check("fixture-refused", await evaluate(qExists("fixture-refused")));
    check("fixture-refused-stays-service", String(await evaluate(qText("mode-banner"))).includes("Service mode"));

    check("no-page-exceptions", pageErrors.length === 0, pageErrors.join(" | ").slice(0, 400));
    check("no-console-errors", consoleErrors.length === 0, consoleErrors.join(" | ").slice(0, 400));

    cdp.ws.close();
  } catch (error) {
    check("acceptance-flow", false, error instanceof Error ? error.message : String(error));
  } finally {
    closeAll();
  }

  const failed = checks.filter((c) => !c.ok);
  console.log(JSON.stringify({ mode: serviceBase === null ? "browser-dead-service" : "browser-live-service", base, failed: failed.length, total: checks.length, checks }, null, 2));
  process.exit(failed.length === 0 ? 0 : 1);
}

async function deadServiceFlow(h) {
  const { cdp, evaluate, waitFor, shot, pageLoad, gotoViaApp } = h;
  // Root redirects to step 1; the dead service surfaces a connection error
  // with retry — never fixtures.
  await pageLoad("/", qExists("service-error"));
  check("root-redirect", (await evaluate("window.location.pathname")) === STEP_ROUTES[0]);
  check("mode-service", String(await evaluate(qText("mode-banner"))).includes("Service mode"));
  const serviceError = String(await evaluate(qText("service-error")));
  check("dead-service-error", /unreachable/i.test(serviceError), serviceError.slice(0, 120));
  check("dead-service-no-fixtures", /never.*fixtures|no fixtures/i.test(serviceError));
  check("click-envs-retry", (await evaluate(clickJS("envs-retry"))) === "ok");
  await shot("step1-dead");

  // Manifest import against the dead service fails honestly.
  check("click-manifest-load", (await evaluate(clickJS("manifest-load"))) === "ok");
  check("click-manifest-import", (await evaluate(clickJS("manifest-import"))) === "ok");
  await waitFor(`document.body.innerText.includes('Manifest rejected')`, "manifest rejection");
  check("manifest-rejected-dead", true);

  // Step 4 renders every control; unsupported limits are disabled at honest
  // values; every input stays labelled.
  await gotoViaApp(STEP_ROUTES[3], qExists("limit-tools"));
  check("set-limit-tools", (await evaluate(setInputJS("limit-tools", "25"))) === "ok");
  check("set-fixture-paid", (await evaluate(setInputJS("fixture-paid", "300000"))) === "ok");
  const disabledModels = await evaluate(`document.querySelector('[data-testid="limit-models"]').disabled === true`);
  const modelsValue = await evaluate(`document.querySelector('[data-testid="limit-models"]').value`);
  check("unsupported-disabled", disabledModels === true && modelsValue === "0", `disabled=${disabledModels} value=${modelsValue}`);
  const disabledParallel = await evaluate(`document.querySelector('[data-testid="limit-parallel"]').disabled === true`);
  const parallelValue = await evaluate(`document.querySelector('[data-testid="limit-parallel"]').value`);
  check("parallel-disabled", disabledParallel === true && parallelValue === "1", `disabled=${disabledParallel} value=${parallelValue}`);
  const unlabeled = await evaluate(
    `(function(){const bad=[];for(const el of document.querySelectorAll('input,textarea')){` +
    `const id=el.id;if(!id||!document.querySelector('label[for="'+id+'"]'))bad.push(el.getAttribute('data-testid')||el.tagName);}` +
    `return bad;})()`,
  );
  check("inputs-labelled", Array.isArray(unlabeled) && unlabeled.length === 0, JSON.stringify(unlabeled));
  await shot("step4-dead");

  // Step 5 shows the audit dispositions for the edited values.
  await gotoViaApp(STEP_ROUTES[4], qExists("effective-config"));
  const config = await evaluate("window.__ghostlab.getConfig()");
  check("review-tools-25", config?.limits?.maxToolCalls === 25);
  check("review-paid", config?.fixture?.paidMinor === 300000);
  check("review-honest-defaults", config?.limits?.maxModelCalls === 0 && config?.limits?.maxParallelRuns === 1);
  check("disposition-tools", String(await evaluate(qText("disposition-limits.maxToolCalls"))).includes("execution-affecting"));
  check("disposition-parallel", String(await evaluate(qText("disposition-limits.maxParallelRuns"))).includes("unsupported"));
  check("disposition-note", String(await evaluate(qText("disposition-agent.note"))).includes("recorded-metadata"));
  await shot("step5-dead");

  // Step 6 run against the dead service: error shown, config preserved.
  await gotoViaApp(STEP_ROUTES[5], qExists("run-button"));
  check("click-run", (await evaluate(clickJS("run-button"))) === "ok");
  await waitFor(qExists("service-error"), "run connection error");
  check("run-dead-error", /unreachable/i.test(String(await evaluate(qText("service-error")))));
  const preserved = await evaluate("window.__ghostlab.getConfig()");
  check("run-config-preserved", preserved?.limits?.maxToolCalls === 25 && preserved?.fixture?.paidMinor === 300000);
  check("run-no-evidence", (await evaluate("window.__ghostlab.getEvidence()")) === null);
  await shot("step6-dead");

  // Narrow viewport: no page-level horizontal overflow.
  await cdp.send("Emulation.setDeviceMetricsOverride", { width: 375, height: 800, deviceScaleFactor: 1, mobile: true });
  await new Promise((r) => setTimeout(r, 500));
  const overflow = await evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth");
  check("narrow-no-overflow", overflow <= 1, `overflow ${overflow}px at 375px`);
  await shot("step6-narrow-dead");
  await cdp.send("Emulation.clearDeviceMetricsOverride");

  // Step 7 without evidence: honest empty state.
  await gotoViaApp(STEP_ROUTES[6], `document.body.innerText.includes('Nothing to export yet')`);
  check("export-empty", true);
  await shot("step7-dead");
}

async function liveServiceFlow(h) {
  const { evaluate, waitFor, shot, pageLoad, gotoViaApp } = h;
  // Root redirects; registered envs come from the live registry.
  await pageLoad("/", qExists("env-select-refund-env"));
  check("root-redirect", (await evaluate("window.location.pathname")) === STEP_ROUTES[0]);
  check("mode-service", String(await evaluate(qText("mode-banner"))).includes("Service mode"));
  check("click-env", (await evaluate(clickJS("env-select-refund-env"))) === "ok");

  // Manifest import binds a verifiable digest; unknown envs are refused.
  check("click-manifest-load", (await evaluate(clickJS("manifest-load"))) === "ok");
  check("click-manifest-import", (await evaluate(clickJS("manifest-import"))) === "ok");
  await waitFor(qExists("manifest-digest"), "manifest digest");
  const digest = String(await evaluate(qText("manifest-digest")));
  check("manifest-digest-shape", /^[0-9a-f]{64}$/.test(digest), digest.slice(0, 16));
  const canonical = await evaluate("window.__ghostlab.getManifestCanonical()");
  const expected = createHash("sha256").update(String(canonical), "utf-8").digest("hex");
  check("manifest-digest-verified", digest === expected, "node:crypto oracle over the page canonical text");
  const unknownManifest = JSON.stringify({ ...JSON.parse(String(await evaluate(`document.querySelector('[data-testid="manifest-text"]').value`))), env_id: "nope-env" });
  check("set-unknown-manifest", (await evaluate(setInputJS("manifest-text", unknownManifest))) === "ok");
  check("click-manifest-import-unknown", (await evaluate(clickJS("manifest-import"))) === "ok");
  await waitFor(`document.body.innerText.includes('Manifest rejected')`, "unknown-env rejection");
  const unknownText = String(await evaluate("document.body.innerText"));
  check("unregistered-refused", /Unknown id|unknown env_id/i.test(unknownText));
  await shot("step1-live");

  // Step 2: with a fresh registry there is no report — qualification
  // executes the env. (A reused registry may already hold a report.)
  await gotoViaApp(STEP_ROUTES[1], `(${qExists("qualification-table")} || ${qExists("qualify-run")})`);
  if ((await evaluate(qExists("qualify-run"))) === true) {
    check("qualify-offered", true);
    check("click-qualify", (await evaluate(clickJS("qualify-run"))) === "ok");
    try {
      await waitFor(qExists("qualification-table"), "qualification table");
    } catch (error) {
      const qualifyFailed = await evaluate(`document.body.innerText.includes('Qualification failed')`);
      const qualifyText = qualifyFailed ? String(await evaluate(qText("qualify-error"))) : "";
      throw new Error(`${error.message} | qualifyFailed=${qualifyFailed} | qualifyError=${qualifyText.slice(0, 400)}`);
    }
  } else {
    check("qualify-offered", true, "report already present in reused registry");
  }
  const qualRows = await evaluate(`document.querySelectorAll('[data-testid="qualification-table"] tbody tr').length`);
  check("qualification-rows", qualRows === 7, `${qualRows} rows`);
  const toolRows = await evaluate(`document.querySelectorAll('[data-testid="tools-table"] tbody tr').length`);
  check("tools-rows", toolRows === 3, `${toolRows} rows`);
  check("qualified-status", String(await evaluate(qText("qualification-label"))).includes("Live service"));
  await shot("step2-live");

  // Step 3: agent selection + operator note.
  await gotoViaApp(STEP_ROUTES[2], qExists("agent-repaired"));
  check("select-repaired", (await evaluate(clickJS("agent-repaired"))) === "ok");
  check("agent-note", (await evaluate(setInputJS("agent-note", "acceptance probe note"))) === "ok");
  await shot("step3-live");

  // Step 4: every control writes the config; unsupported limits disabled.
  await gotoViaApp(STEP_ROUTES[3], qExists("limit-tools"));
  check("set-limit-tools", (await evaluate(setInputJS("limit-tools", "25"))) === "ok");
  check("set-fixture-paid", (await evaluate(setInputJS("fixture-paid", "300000"))) === "ok");
  check("set-objective", (await evaluate(setInputJS("objective", "Acceptance objective"))) === "ok");
  check("uncheck-invariant", (await evaluate(clickJS("check-unique_idempotency_keys"))) === "ok");
  await new Promise((r) => setTimeout(r, 500));
  check("no-config-errors", (await evaluate(qExists("config-errors"))) === false);
  const disabledModels = await evaluate(`document.querySelector('[data-testid="limit-models"]').disabled === true`);
  check("unsupported-disabled", disabledModels === true);
  const unlabeled = await evaluate(
    `(function(){const bad=[];for(const el of document.querySelectorAll('input,textarea')){` +
    `const id=el.id;if(!id||!document.querySelector('label[for="'+id+'"]'))bad.push(el.getAttribute('data-testid')||el.tagName);}` +
    `return bad;})()`,
  );
  check("inputs-labelled", Array.isArray(unlabeled) && unlabeled.length === 0, JSON.stringify(unlabeled));
  await shot("step4-live");

  // Step 5: review + audit dispositions.
  await gotoViaApp(STEP_ROUTES[4], qExists("effective-config"));
  const config = await evaluate("window.__ghostlab.getConfig()");
  check("review-tools-25", config?.limits?.maxToolCalls === 25);
  check("review-adapter", config?.agent?.adapter === "repaired");
  check("review-note", config?.agent?.note === "acceptance probe note");
  check("review-paid", config?.fixture?.paidMinor === 300000);
  check("review-objective", config?.objective === "Acceptance objective");
  check("disposition-tools", String(await evaluate(qText("disposition-limits.maxToolCalls"))).includes("execution-affecting"));
  check("disposition-parallel", String(await evaluate(qText("disposition-limits.maxParallelRuns"))).includes("unsupported"));
  await shot("step5-live");

  // Step 6: secret-like notes are refused before any traffic.
  await gotoViaApp(STEP_ROUTES[5], qExists("run-button"));
  await gotoViaApp(STEP_ROUTES[2], qExists("agent-repaired"));
  check("agent-note-secret", (await evaluate(setInputJS("agent-note", "leak API_KEY=secret123"))) === "ok");
  await gotoViaApp(STEP_ROUTES[5], qExists("run-button"));
  check("click-run-secret", (await evaluate(clickJS("run-button"))) === "ok");
  await waitFor(`document.body.innerText.includes('secret-like')`, "secret prescan refusal");
  check("secret-refused", true);
  check("secret-no-evidence", (await evaluate("window.__ghostlab.getEvidence()")) === null);
  await gotoViaApp(STEP_ROUTES[2], qExists("agent-repaired"));
  check("agent-note-fixed", (await evaluate(setInputJS("agent-note", "acceptance probe note"))) === "ok");
  await gotoViaApp(STEP_ROUTES[5], qExists("run-button"));

  const waitEvidenceChange = async (before, label) => {
    const start = Date.now();
    for (;;) {
      const current = await evaluate("window.__ghostlab.getEvidence()?.submittedAt ?? null");
      if (current !== null && current !== before) return;
      if (Date.now() - start > 90000) throw new Error(`timed out waiting for ${label}`);
      await new Promise((r) => setTimeout(r, 500));
    }
  };

  // Main run: 3 arms execute; the paid change flows into every ledger.
  check("click-run", (await evaluate(clickJS("run-button"))) === "ok");
  await waitFor(qExists("recorded-config"), "recorded config");
  const evidence = await evaluate("window.__ghostlab.getEvidence()");
  check("evidence-mode", evidence?.mode === "live", String(evidence?.mode));
  check("evidence-arms", evidence?.references?.arms?.length === 3, JSON.stringify(evidence?.references?.arms?.map((a) => a.variant)));
  const bodyText = String(await evaluate("document.body.innerText"));
  check("total-baseline", bodyText.includes("300,000"), "paid change reached the baseline ledger");
  check("total-faulted", bodyText.includes("600,000"), "faulted ledger doubled the paid amount");
  const arms = evidence?.references?.arms ?? [];
  const faulted = arms.find((a) => a.variant === "faulted");
  check("finding-linked", evidence?.finding?.run_id === faulted?.runId, "finding cites the executed faulted run, nothing invented");
  check("provenance-target", String(evidence?.provenance?.target).includes("deterministic-adapter"));
  check("provenance-calls", evidence?.provenance?.modelCalls === 0);
  check("provenance-spend", String(evidence?.provenance?.spend).includes("no paid calls"));
  check("mapping-notes", String(await evaluate(qText("mapping-notes"))).includes("repetitions"));
  const repairedTrace = JSON.stringify(evidence?.result?.repaired?.trace ?? []);
  const faultedTrace = JSON.stringify(evidence?.result?.faulted?.trace ?? []);
  check("repaired-behavior", repairedTrace.includes("get_refund_status") && !faultedTrace.includes("get_refund_status"), "repaired adapter checks status before retrying");
  const armRows = await evaluate(`document.querySelectorAll('[data-testid="arms-table"] tbody tr').length`);
  check("arms-rows", armRows === 3, `${armRows} rows`);
  await shot("step6-live");

  // Lowered budget: the faulted arm cannot fit and goes incomplete.
  // (Each re-run carries a distinct objective name: the run route cannot
  // start a new attempt for an identical config.)
  await gotoViaApp(STEP_ROUTES[3], qExists("limit-tools"));
  check("set-limit-tools-2", (await evaluate(setInputJS("limit-tools", "2"))) === "ok");
  check("set-objective-budget", (await evaluate(setInputJS("objective", "Acceptance objective (budget probe)"))) === "ok");
  await gotoViaApp(STEP_ROUTES[5], qExists("run-button"));
  const beforeBudget = await evaluate("window.__ghostlab.getEvidence()?.submittedAt ?? null");
  check("click-run-budget", (await evaluate(clickJS("run-button"))) === "ok");
  await waitEvidenceChange(beforeBudget, "budget evidence");
  const budgetEvidence = await evaluate("window.__ghostlab.getEvidence()");
  const budgetFaulted = (budgetEvidence?.references?.arms ?? []).find((a) => a.variant === "faulted");
  check("budget-arm-status", budgetFaulted?.status === "budget_exhausted", String(budgetFaulted?.status));
  check("budget-arm-verdict", budgetFaulted?.verdict === "incomplete", String(budgetFaulted?.verdict));
  await gotoViaApp(STEP_ROUTES[3], qExists("limit-tools"));
  check("set-limit-tools-back", (await evaluate(setInputJS("limit-tools", "25"))) === "ok");

  // Disabled timeout: the submitted variant matches no canonical branch and
  // executes separately with no fault armed (verdict pass).
  check("set-objective-nofault", (await evaluate(setInputJS("objective", "Acceptance objective (no-fault probe)"))) === "ok");
  check("uncheck-timeout", (await evaluate(clickJS("fault-timeout"))) === "ok");
  await gotoViaApp(STEP_ROUTES[5], qExists("run-button"));
  const beforeNoFault = await evaluate("window.__ghostlab.getEvidence()?.submittedAt ?? null");
  check("click-run-nofault", (await evaluate(clickJS("run-button"))) === "ok");
  await waitEvidenceChange(beforeNoFault, "no-fault evidence");
  const noFaultEvidence = await evaluate("window.__ghostlab.getEvidence()");
  const extra = (noFaultEvidence?.references?.arms ?? []).find((a) => a.variant === "submitted-extra");
  check("nofault-extra-arm", extra !== undefined && extra.verdict === "pass", JSON.stringify(extra ?? null).slice(0, 120));
  await gotoViaApp(STEP_ROUTES[3], qExists("limit-tools"));
  check("set-objective-final", (await evaluate(setInputJS("objective", "Acceptance objective (final)"))) === "ok");
  check("check-timeout", (await evaluate(clickJS("fault-timeout"))) === "ok");
  await gotoViaApp(STEP_ROUTES[5], qExists("run-button"));
  const beforeFinal = await evaluate("window.__ghostlab.getEvidence()?.submittedAt ?? null");
  check("click-run-final", (await evaluate(clickJS("run-button"))) === "ok");
  await waitEvidenceChange(beforeFinal, "final evidence");

  // Narrow viewport: no page-level horizontal overflow.
  await h.cdp.send("Emulation.setDeviceMetricsOverride", { width: 375, height: 800, deviceScaleFactor: 1, mobile: true });
  await new Promise((r) => setTimeout(r, 500));
  const overflow = await evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth");
  check("narrow-no-overflow", overflow <= 1, `overflow ${overflow}px at 375px`);
  await shot("step6-narrow-live");
  await h.cdp.send("Emulation.clearDeviceMetricsOverride");

  // Step 7: server export of the selected run, server compare, retest split.
  await gotoViaApp(STEP_ROUTES[6], qExists("export-fetch"));
  check("export-arms", (await evaluate(qExists("export-arm-baseline"))) === true && (await evaluate(qExists("export-arm-faulted"))) === true);
  check("click-export-fetch", (await evaluate(clickJS("export-fetch"))) === "ok");
  await waitFor(qExists("export-table"), "export table");
  const exportRows = await evaluate(`document.querySelectorAll('[data-testid="export-table"] tbody tr').length`);
  check("export-files", exportRows === 5, `${exportRows} files`);
  const exportPreview = String(await evaluate(qText("export-preview")));
  check("export-carries-runs", exportPreview.includes("run-") || exportPreview.includes("run_id"));
  const readmePreview = String(await evaluate(qText("readme-preview")));
  check("export-echo-match", readmePreview.includes("matches the faulted variant: yes"));
  const exportText = `${exportPreview}\n${readmePreview}`;
  check("export-no-secrets", !SECRET_PATTERNS.some((re) => re.test(exportText)));
  check("click-compare", (await evaluate(clickJS("compare-run"))) === "ok");
  await waitFor(qExists("compare-result"), "compare result");
  check("compare-diffs", String(await evaluate(qText("compare-result"))).includes("effective.faults") || String(await evaluate(qText("compare-result"))).includes("diffs:"));
  await shot("step7-live");

  // Reload recovery: a full load restores the newest verified run via API.
  try {
    await pageLoad(STEP_ROUTES[5], qExists("recorded-config"));
  } catch (error) {
    const storageKeys = await evaluate("Object.keys(sessionStorage).join(',')");
    const recoveryTable = await evaluate(`document.querySelector('[data-testid="recovery-table"]')!==null`);
    const evidenceNow = await evaluate("window.__ghostlab.getEvidence()!==null");
    throw new Error(`${error.message} | sessionKeys=${storageKeys} | recoveryTable=${recoveryTable} | evidence=${evidenceNow}`);
  }
  const recovered = await evaluate("window.__ghostlab.getEvidence()");
  check("reload-recovers", recovered !== null && recovered.mode === "live");
  check("reload-historical", String(recovered?.provenance?.evidence).includes("historical") && String(recovered?.provenance?.evidence).includes("digests match"));
  check("reload-config", (await evaluate("window.__ghostlab.getConfig()"))?.fixture?.paidMinor === 300000);

  // Dead backend override: connection error, config preserved, no fixtures.
  // (Port 18917: outside Chrome's unsafe-port list, nothing listening, so
  // the browser attempts a real connection and gets a refusal.)
  await pageLoad(`/guide/1-environment?api=${encodeURIComponent("http://127.0.0.1:18917")}`, qExists("service-error"));
  const overrideError = String(await evaluate(qText("service-error")));
  check("override-dead", /unreachable/i.test(overrideError));
  check("override-banner", String(await evaluate(qText("mode-banner"))).includes("127.0.0.1:18917"));

  // Fresh retest: new execution with new ids (not a replay).
  await pageLoad(STEP_ROUTES[6], qExists("export-fetch"));
  const beforeRetest = await evaluate("window.__ghostlab.getEvidence()?.submittedAt ?? null");
  check("click-retest", (await evaluate(clickJS("retest-run"))) === "ok");
  await waitEvidenceChange(beforeRetest, "retest evidence");
  check("retest-navigated", (await evaluate("window.location.pathname")) === STEP_ROUTES[5]);
  const retested = await evaluate("window.__ghostlab.getEvidence()");
  check("retest-fresh", retested?.submittedAt !== beforeRetest && (retested?.references?.arms?.length ?? 0) === 3);
  await shot("step7-retest");
}

main();

