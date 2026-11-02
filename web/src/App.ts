/* Track D guided-run app shell: path router, shared state, layout. */

import { createElement, useEffect, useState, type ReactNode } from "react";
import { FixtureExperimentApi } from "./api";
import { defaultConfig, setPath, GUIDED_STATE_VERSION } from "./config";
import { Step1 } from "./screens/step1";
import { Step2 } from "./screens/step2";
import { Step3 } from "./screens/step3";
import { Step4 } from "./screens/step4";
import { Step5 } from "./screens/step5";
import { Step6 } from "./screens/step6";
import { Step7 } from "./screens/step7";
import type { ExperimentEvidence } from "./types";
import {
  banner,
  div,
  footer,
  h1,
  header,
  main,
  navLink,
  p,
  small,
  stepper,
  type StepContext,
} from "./ui";

const STEPS = [
  { path: "/guide/1-environment", title: "Select environment & import manifest", component: Step1 },
  { path: "/guide/2-qualification", title: "Inspect qualification & limits", component: Step2 },
  { path: "/guide/3-agent", title: "Select agent", component: Step3 },
  { path: "/guide/4-configure", title: "Configure fixtures, faults, checks & limits", component: Step4 },
  { path: "/guide/5-review", title: "Review effective config", component: Step5 },
  { path: "/guide/6-run", title: "Run & inspect evidence", component: Step6 },
  { path: "/guide/7-export", title: "Export, compare & retest", component: Step7 },
] as const;

export const STEP_PATHS: readonly string[] = STEPS.map((s) => s.path);

function normalize(path: string): string {
  if (path.length > 1 && path.endsWith("/")) return path.slice(0, -1);
  return path;
}

function currentPath(): string {
  return normalize(window.location.pathname);
}

interface GhostlabHook {
  version: string;
  getConfig: () => unknown;
  getEvidence: () => unknown;
  getExportPreview: () => unknown;
  getManifestCanonical: () => unknown;
}

function NotFound(props: { navigate: (path: string) => void }): ReactNode {
  return div(
    null,
    h1(null, "Unknown guided-run route"),
    banner("warn", "No step here", "Pick a step below; the guide keeps no state in unknown routes."),
    p(null, navLink(props.navigate, STEPS[0].path, "← Start at step 1")),
  );
}

export function App(): ReactNode {
  const [route, setRoute] = useState<string>(() => currentPath());
  const [config, setConfig] = useState(defaultConfig);
  const [evidence, setEvidence] = useState<ExperimentEvidence | null>(null);
  const [manifestText, setManifestText] = useState("");
  const [importDigest, setImportDigest] = useState<string | null>(null);
  const [importCanonical, setImportCanonical] = useState<string | null>(null);
  const [notices, setNotices] = useState<readonly string[]>([]);
  const [api] = useState(() => new FixtureExperimentApi());

  const navigate = (path: string): void => {
    window.history.pushState({}, "", path);
    window.scrollTo(0, 0);
    setRoute(normalize(path));
  };

  useEffect(() => {
    const onPop = (): void => setRoute(currentPath());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    if (route === "/") {
      window.history.replaceState({}, "", STEPS[0].path);
      setRoute(STEPS[0].path);
    }
  }, [route]);

  // Acceptance hook (documented in docs/modules/M08.md). Read-only state for
  // the browser suite; carries no credentials and grants no authority.
  useEffect(() => {
    const hook: GhostlabHook = {
      version: GUIDED_STATE_VERSION,
      getConfig: () => config,
      getEvidence: () => evidence,
      getExportPreview: () => (evidence === null ? null : api.exportBundle(evidence)),
      getManifestCanonical: () => importCanonical,
    };
    (window as unknown as { __ghostlab?: GhostlabHook }).__ghostlab = hook;
  });

  const step = STEPS.find((s) => s.path === route);

  const ctx: StepContext = {
    config,
    api,
    evidence,
    setEvidence,
    manifestText,
    setManifestText,
    importDigest,
    importCanonical,
    setImport: (digest, canonical) => {
      setImportDigest(digest);
      setImportCanonical(canonical);
    },
    update: (path, value) => setConfig((prev) => setPath(prev, path, value)),
    notices,
    notify: (message) => setNotices((prev) => [...prev.slice(-4), message]),
    navigate,
  };

  return div(
    { className: "app" },
    header(
      { className: "app-header" },
      h1(null, "GhostLab guided run"),
      p({ className: "mode", "data-testid": "mode-banner" }, "Fixture mode: scripted results until the service lands. No credentials in the browser."),
      stepper(STEPS.map((s) => ({ path: s.path, title: s.title })), route, navigate),
    ),
    main(
      { className: "app-main" },
      ...notices.map((notice, index) => banner("ok", notice, undefined)),
      step === undefined
        ? createElement(NotFound, { navigate })
        : createElement(step.component, { ctx }),
    ),
    footer(
      { className: "app-footer" },
      small(
        null,
        "Fixtures are dev aids, never final evidence. Financial verdicts come from executed evidence. ",
        "Guide: docs/modules/M08.md.",
      ),
    ),
  );
}
