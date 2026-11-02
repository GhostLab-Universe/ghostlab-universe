/* Track D guided-run app shell: path router, shared state, layout.
 *
 * Production path is service-first: the app instantiates ServiceGuideApi
 * (LiveExperimentApi transport + reference/provenance assembly) and never
 * falls back to fixtures — a dead service surfaces a connection error with
 * the config preserved for retry. Fixtures exist only behind the explicit
 * dev-only `?fixture=1` flag under `vite dev`; production builds drop that
 * branch (and the fixture chunk) at compile time. */

import { createElement, useEffect, useRef, useState, type ReactNode } from "react";
import { LiveExperimentApi, type ExperimentApi } from "./api";
import { ServiceGuideApi, isServiceGuide, type RecoveryOutcome } from "./serviceClient";
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

/** Same-origin by default; explicit ?api= override for dev/test. */
function serviceBase(): string {
  const override = new URLSearchParams(window.location.search).get("api");
  return (override ?? "").replace(/\/+$/, "");
}

function wantsFixture(): boolean {
  return new URLSearchParams(window.location.search).get("fixture") === "1";
}

function makeServiceApi(base: string): ServiceGuideApi {
  return new ServiceGuideApi({ live: new LiveExperimentApi(base), baseUrl: base });
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
  const [api, setApi] = useState<ExperimentApi | ServiceGuideApi | null>(null);
  const [fixtureRefused, setFixtureRefused] = useState(false);
  const [recovery, setRecovery] = useState<readonly RecoveryOutcome[]>([]);
  // Ref, not state: flipping state here would re-render, run this effect's
  // cleanup, and cancel the in-flight recovery before it completes.
  const recoveryStarted = useRef(false);
  const [base] = useState<string>(() => serviceBase());

  // Resolve the api once: service by default; fixtures only via the
  // explicit dev-only flag. The DEV branch below is dead code in
  // production builds, so the fixture chunk is never emitted there.
  useEffect(() => {
    let cancelled = false;
    if (!wantsFixture()) {
      setApi(makeServiceApi(base));
      return;
    }
    if (import.meta.env.DEV) {
      import("./api").then(
        (mod) => {
          if (!cancelled) setApi(new mod.FixtureExperimentApi());
        },
        () => {
          if (!cancelled) setApi(makeServiceApi(base));
        },
      );
      return () => {
        cancelled = true;
      };
    }
    setFixtureRefused(true);
    setApi(makeServiceApi(base));
  }, [base]);

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

  // Reload recovery (service only): re-read stored groups via the API,
  // verify digests, and restore the newest verified run.
  useEffect(() => {
    if (api === null || !isServiceGuide(api) || recoveryStarted.current) return;
    recoveryStarted.current = true;
    let cancelled = false;
    api
      .recoverStored()
      .then((outcomes) => {
        if (cancelled) return;
        setRecovery(outcomes);
        const verified = outcomes.filter((o) => o.verified);
        const latest = verified[verified.length - 1];
        if (latest !== undefined) {
          setConfig(JSON.parse(JSON.stringify(latest.stored.submittedConfig)) as typeof config);
          setEvidence(JSON.parse(JSON.stringify(latest.stored.evidence)) as ExperimentEvidence);
          setNotices((prev) => [
            ...prev.slice(-4),
            `Recovered the newest verified stored run (${latest.stored.arms.length} arm(s), digests match).`,
          ]);
        } else if (outcomes.length > 0) {
          setNotices((prev) => [
            ...prev.slice(-4),
            "Stored runs exist but none verified against the live service; see step 6 for details.",
          ]);
        }
      })
      .catch(() => {
        if (!cancelled) setRecovery([]);
      });
    return () => {
      cancelled = true;
    };
  }, [api]);

  const refreshRecovery = (): void => {
    if (api === null || !isServiceGuide(api)) return;
    api
      .recoverStored()
      .then((outcomes) => setRecovery(outcomes))
      .catch(() => setRecovery([]));
  };

  // Acceptance hook (documented in docs/modules/M08.md). Read-only state for
  // the browser suite; carries no credentials and grants no authority.
  useEffect(() => {
    const hook: GhostlabHook = {
      version: GUIDED_STATE_VERSION,
      getConfig: () => config,
      getEvidence: () => evidence,
      getExportPreview: () => {
        if (evidence === null || api === null || isServiceGuide(api)) return null;
        return api.exportBundle(evidence);
      },
      getManifestCanonical: () => importCanonical,
    };
    (window as unknown as { __ghostlab?: GhostlabHook }).__ghostlab = hook;
  });

  const step = STEPS.find((s) => s.path === route);

  const serviceInfo =
    api !== null && isServiceGuide(api)
      ? { baseUrl: api.serviceBaseUrl, projectId: api.projectId(), projectObserved: api.projectObserved() }
      : null;

  const ctx: StepContext = {
    config,
    api: api ?? makeServiceApi(base),
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
    replaceConfig: (next) => setConfig(JSON.parse(JSON.stringify(next)) as typeof config),
    notices,
    notify: (message) => setNotices((prev) => [...prev.slice(-4), message]),
    navigate,
    service: serviceInfo,
    recovery,
    refreshRecovery,
  };

  const modeBanner =
    api === null
      ? p({ className: "mode", "data-testid": "mode-banner" }, "Starting…")
      : isServiceGuide(api)
        ? p(
            { className: "mode", "data-testid": "mode-banner" },
            `Service mode: results execute on ${serviceInfo?.baseUrl === "" ? "the same-origin service" : serviceInfo?.baseUrl} ` +
              `(project ${serviceInfo?.projectId ?? "?"}${serviceInfo?.projectObserved === true ? "" : ", client default — confirmed on first call"}). ` +
              "Connection errors never show fixtures. No credentials in the browser.",
          )
        : p(
            { className: "mode", "data-testid": "mode-banner" },
            "DEV-ONLY fixture mode (?fixture=1 under vite dev): approved/illustrative fixtures, never evidence. Production builds cannot reach this path.",
          );

  return div(
    { className: "app" },
    header(
      { className: "app-header" },
      h1(null, "GhostLab guided run"),
      modeBanner,
      stepper(STEPS.map((s) => ({ path: s.path, title: s.title })), route, navigate),
    ),
    main(
      { className: "app-main" },
      fixtureRefused
        ? banner(
            "error",
            "Dev fixtures unavailable in this build",
            "The ?fixture=1 flag needs `vite dev`; this production build stays in service mode. No fixtures were loaded.",
          )
        : null,
      ...(fixtureRefused ? [p({ "data-testid": "fixture-refused" }, "fixture-refused")] : []),
      ...notices.map((notice) => banner("ok", notice, undefined)),
      step === undefined
        ? createElement(NotFound, { navigate })
        : api === null
          ? p(null, "Starting…")
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
