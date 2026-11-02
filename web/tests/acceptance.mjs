#!/usr/bin/env node
// Track D production acceptance: serves web/dist over 127.0.0.1, drives the
// real guided routes in headless Chrome via CDP, and verifies the 7-step flow
// end to end. Stdlib only (fetch + WebSocket + CDP); no new dependencies.
// State stays in a track-private os.tmpdir() directory; nothing is written
// to the repo.
//
//   node web/tests/acceptance.mjs [--dist=web/dist] [--chrome=PATH]
//     [--allow-static] [--keep-temp]
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
  "Fixture mode",
  "__ghostlab",
  "scripted",
  "recorded-config",
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
}

const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".json": "application/json", ".txt": "text/plain" };

function startServer() {
  return new Promise((resolve) => {
    const server = createServer(async (req, res) => {
      try {
        const url = new URL(req.url ?? "/", "http://127.0.0.1");
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
        res.writeHead(500);
        res.end("error");
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
      if (p?.entry?.level === "error") consoleErrors.push(String(p.entry.text).slice(0, 300));
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

    // Root redirects to step 1; manifest import binds a verifiable digest.
    await pageLoad("/", qExists("manifest-text"));
    check("root-redirect", (await evaluate("window.location.pathname")) === STEP_ROUTES[0]);
    check("click-manifest-load", (await evaluate(clickJS("manifest-load"))) === "ok");
    const preset = await evaluate(
      `(function(){const el=document.querySelector('[data-testid="manifest-text"]');return el?el.value.length:0;})()`,
    );
    check("manifest-loaded", preset > 100, `${preset} chars`);
    check("click-manifest-import", (await evaluate(clickJS("manifest-import"))) === "ok");
    await waitFor(qExists("manifest-digest"), "manifest digest");
    const digest = String(await evaluate(qText("manifest-digest")));
    check("manifest-digest-shape", /^[0-9a-f]{64}$/.test(digest), digest.slice(0, 16));
    const canonical = await evaluate("window.__ghostlab.getManifestCanonical()");
    const expected = createHash("sha256").update(String(canonical), "utf-8").digest("hex");
    check("manifest-digest-verified", digest === expected, "node:crypto oracle over the page canonical text");
    await shot("step1");

    // Step 2: qualification + limits inspection.
    await gotoViaApp(STEP_ROUTES[1], qExists("qualification-table"));
    const qualRows = await evaluate(`document.querySelectorAll('[data-testid="qualification-table"] tbody tr').length`);
    check("qualification-rows", qualRows === 7, `${qualRows} rows`);
    const toolRows = await evaluate(`document.querySelectorAll('[data-testid="tools-table"] tbody tr').length`);
    check("tools-rows", toolRows === 3, `${toolRows} rows`);
    check("illustrative-label", String(await evaluate(qText("qualification-label"))).includes("Illustrative"));
    await shot("step2");

    // Step 3: agent selection is recorded, inference labeled scripted.
    await gotoViaApp(STEP_ROUTES[2], qExists("agent-repaired"));
    check("select-repaired", (await evaluate(clickJS("agent-repaired"))) === "ok");
    check("agent-note", (await evaluate(setInputJS("agent-note", "acceptance probe note"))) === "ok");
    await shot("step3");

    // Step 4: every control writes the config; validation stays green.
    await gotoViaApp(STEP_ROUTES[3], qExists("limit-tools"));
    check("set-limit-tools", (await evaluate(setInputJS("limit-tools", "25"))) === "ok");
    check("set-fixture-paid", (await evaluate(setInputJS("fixture-paid", "300000"))) === "ok");
    check("set-objective", (await evaluate(setInputJS("objective", "Acceptance objective"))) === "ok");
    check("uncheck-invariant", (await evaluate(clickJS("check-unique_idempotency_keys"))) === "ok");
    await new Promise((r) => setTimeout(r, 500));
    check("no-config-errors", (await evaluate(qExists("config-errors"))) === false);
    const unlabeled = await evaluate(
      `(function(){const bad=[];for(const el of document.querySelectorAll('input,textarea')){` +
      `const id=el.id;if(!id||!document.querySelector('label[for="'+id+'"]'))bad.push(el.getAttribute('data-testid')||el.tagName);}` +
      `return bad;})()`,
    );
    check("inputs-labelled", Array.isArray(unlabeled) && unlabeled.length === 0, JSON.stringify(unlabeled));
    await shot("step4");

    // Step 5: review shows every submitted value + derived payloads.
    await gotoViaApp(STEP_ROUTES[4], qExists("effective-config"));
    const config = await evaluate("window.__ghostlab.getConfig()");
    check("review-tools-25", config?.limits?.maxToolCalls === 25);
    check("review-adapter", config?.agent?.adapter === "repaired");
    check("review-note", config?.agent?.note === "acceptance probe note");
    check("review-paid", config?.fixture?.paidMinor === 300000);
    check("review-objective", config?.objective === "Acceptance objective");
    check(
      "review-invariants",
      Array.isArray(config?.checks?.invariants) &&
        config.checks.invariants.length === 3 &&
        !config.checks.invariants.includes("unique_idempotency_keys"),
      JSON.stringify(config?.checks?.invariants),
    );
    check("review-digest", config?.env?.manifestDigest === digest);
    check("campaign-preview", String(await evaluate(qText("campaign-spec"))).includes('"max_tool_calls": 25'));
    check("proposal-preview", String(await evaluate(qText("experiment-proposal"))).includes('"repaired"'));
    await shot("step5");

    // Step 6: run records the config; evidence shows fixture totals + divergence.
    await gotoViaApp(STEP_ROUTES[5], qExists("run-button"));
    check("click-run", (await evaluate(clickJS("run-button"))) === "ok");
    await waitFor(qExists("recorded-config"), "recorded config");
    const evidence = await evaluate("window.__ghostlab.getEvidence()");
    check("evidence-mode", evidence?.mode === "fixture-scripted", String(evidence?.mode));
    check("evidence-records-config", JSON.stringify(evidence?.effectiveConfig) === JSON.stringify(config));
    const bodyText = String(await evaluate("document.body.innerText"));
    check("total-baseline", bodyText.includes("250,000"), "baseline total rendered");
    check("total-faulted", bodyText.includes("500,000"), "faulted total rendered");
    check("divergence-event-2", bodyText.includes("event 2"), "first divergence rendered");
    check("scripted-label", bodyText.includes("Scripted run"), "scripted inference labeled honestly");
    await shot("step6");

    // Narrow viewport: no page-level horizontal overflow.
    await cdp.send("Emulation.setDeviceMetricsOverride", { width: 375, height: 800, deviceScaleFactor: 1, mobile: true });
    await new Promise((r) => setTimeout(r, 500));
    const overflow = await evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth");
    check("narrow-no-overflow", overflow <= 1, `overflow ${overflow}px at 375px`);
    await shot("step6-narrow");
    await cdp.send("Emulation.clearDeviceMetricsOverride");

    // Step 7: export bundle carries the config; capsule replays for real.
    await gotoViaApp(STEP_ROUTES[6], qExists("export-preview"));
    const preview = await evaluate("window.__ghostlab.getExportPreview()");
    const evidenceFile = preview?.files?.find((f) => f.name === "evidence.json");
    const exported = JSON.parse(evidenceFile.content);
    check("export-carries-config", exported?.effectiveConfig?.limits?.maxToolCalls === 25);
    check("export-mode", exported?.mode === "fixture-scripted");
    const capsuleFile = preview?.files?.find((f) => f.name === "capsule-fixture.json");
    const capsulePath = path.join(temp, "capsule-fixture.json");
    await fs.writeFile(capsulePath, capsuleFile.content);
    const replayOut = await runReplay(capsulePath);
    const replay = JSON.parse(replayOut);
    check("exported-capsule-replays", replay?.reproduced === true && replay?.model_calls === 0, replayOut.trim().slice(0, 120));
    const exportText = preview.files.map((f) => f.content).join("\n");
    check("export-no-secrets", !SECRET_PATTERNS.some((re) => re.test(exportText)));
    await shot("step7");

    // Full-load routes: deep links serve the app shell; fresh loads reset to
    // defaults; unknown routes render NotFound instead of breaking.
    await pageLoad("/guide/4-configure", qExists("limit-tools"));
    check("deep-link-route", (await evaluate("window.location.pathname")) === "/guide/4-configure");
    const fresh = await evaluate("window.__ghostlab.getConfig()");
    check("deep-link-defaults", fresh?.limits?.maxToolCalls === 20, "fresh load starts from defaults");
    await pageLoad("/guide/99-nope", `document.body.innerText.includes('Unknown guided-run route')`);
    check("unknown-route", await evaluate(`document.body.innerText.includes('Unknown guided-run route')`));

    check("no-page-exceptions", pageErrors.length === 0, pageErrors.join(" | ").slice(0, 400));
    check("no-console-errors", consoleErrors.length === 0, consoleErrors.join(" | ").slice(0, 400));

    cdp.ws.close();
  } catch (error) {
    check("acceptance-flow", false, error instanceof Error ? error.message : String(error));
  } finally {
    closeAll();
  }

  const failed = checks.filter((c) => !c.ok);
  console.log(JSON.stringify({ mode: "browser", base, failed: failed.length, total: checks.length, checks }, null, 2));
  process.exit(failed.length === 0 ? 0 : 1);
}

main();
