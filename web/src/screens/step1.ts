/* Step 1: select an environment and import its manifest. */

import { useEffect, useState, type ReactNode } from "react";
import { ILLUSTRATIVE_MANIFEST_TEXT } from "../fixtures";
import { isServiceGuide } from "../serviceClient";
import type { EnvSummary } from "../types";
import {
  banner,
  button,
  code,
  codeBlock,
  div,
  field,
  h,
  h1,
  h2,
  navLink,
  p,
  radioRow,
  small,
  textArea,
  type StepContext,
} from "../ui";

export function Step1(props: { ctx: StepContext }): ReactNode {
  const { ctx } = props;
  const [envs, setEnvs] = useState<EnvSummary[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);
  const service = isServiceGuide(ctx.api);

  useEffect(() => {
    let cancelled = false;
    setLoadError(null);
    setEnvs(null);
    ctx.api
      .listEnvs()
      .then((list) => {
        if (!cancelled) setEnvs(list);
      })
      .catch((error: unknown) => {
        if (!cancelled) setLoadError(error instanceof Error ? error.message : String(error));
      });
    return () => {
      cancelled = true;
    };
  }, [ctx.api, reloadToken]);

  const doImport = (): void => {
    setImporting(true);
    setImportError(null);
    ctx.api
      .importManifest(ctx.config.env.envId, ctx.manifestText)
      .then((result) => {
        ctx.setImport(result.manifestDigest, result.canonicalText);
        ctx.update("env.manifestDigest", result.manifestDigest);
        ctx.notify(`Manifest imported for ${result.envId}.`);
      })
      .catch((error: unknown) => {
        ctx.setImport(null, null);
        ctx.update("env.manifestDigest", null);
        setImportError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => setImporting(false));
  };

  const envPanel =
    loadError !== null
      ? div(
          null,
          banner("error", service ? "Service unreachable or refusing" : "Could not list environments", loadError),
          p({ "data-testid": "service-error" }, loadError),
          service
            ? div(
                null,
                p(null, "The config is preserved. Start the service and retry — fixtures are never substituted."),
                button({ onClick: () => setReloadToken((n) => n + 1), label: "Retry", testid: "envs-retry" }),
              )
            : null,
        )
      : envs === null
        ? p(null, "Loading environments…")
        : envs.length === 0
          ? div(
              null,
              h2(null, "Environment"),
              p({ "data-testid": "envs-empty" }, "No registered environments."),
              banner(
                "warn",
                "Empty registry: setup needed",
                "0 configured environments. Installing adapter code is an explicit operator act " +
                  "(ghostlab env-register); the service exposes no registration route. " +
                  "A code-supported list is not exposed by GET /v1/envs, so only configured envs are shown here.",
              ),
              service
                ? button({ onClick: () => setReloadToken((n) => n + 1), label: "Refresh", testid: "envs-retry" })
                : null,
            )
          : div(
              null,
              h2(null, "Environment"),
              ...envs.map((env) =>
                div(
                  { key: env.envId },
                  radioRow({
                    name: "env",
                    id: `env-${env.envId}`,
                    label: `${env.displayName} (${env.envId}, ${env.envVersion}) — ${env.status}`,
                    checked: ctx.config.env.envId === env.envId,
                    onChange: () => {
                      ctx.update("env.envId", env.envId);
                      ctx.update("env.envVersion", env.envVersion);
                      ctx.update("env.adapterRef", env.adapterRef);
                    },
                    testid: `env-select-${env.envId}`,
                    configPath: "env.envId",
                  }),
                  small(null, env.label),
                ),
              ),
            );

  return div(
    null,
    h1(null, "Step 1: Select environment & import manifest"),
    service
      ? banner(
          "info",
          "Service environments",
          `Registered envs from ${ctx.service?.baseUrl === "" ? "the same-origin service" : ctx.service?.baseUrl} ` +
            `(project ${ctx.service?.projectId ?? "?"}). Unregistered envs are refused; nothing here is a dev aid.`,
        )
      : banner("info", "Fixture mode", "Environment data below is an illustrative dev aid until the Track A service lands."),
    envPanel,
    h2(null, "Manifest"),
    p(
      null,
      "Paste a v1 JSON manifest for the selected environment. Import validates the shape, ",
      "checks it against the registration, and binds the canonical SHA-256 digest to the config. ",
      "Import never executes the environment.",
    ),
    field(
      "Manifest JSON",
      textArea({
        id: "manifest-text",
        rows: 12,
        value: ctx.manifestText,
        onChange: ctx.setManifestText,
        testid: "manifest-text",
      }),
      { hint: "Exact v1 fields only; no URLs, paths, packages or code references.", htmlFor: "manifest-text" },
    ),
    div(
      { className: "row" },
      button({ onClick: () => ctx.setManifestText(ILLUSTRATIVE_MANIFEST_TEXT), label: "Load illustrative manifest", testid: "manifest-load" }),
      button({ onClick: doImport, label: importing ? "Importing…" : "Import manifest", disabled: importing, testid: "manifest-import" }),
    ),
    importError !== null ? banner("error", "Manifest rejected", importError) : null,
    ctx.importDigest !== null
      ? div(
          null,
          h2(null, "Imported manifest"),
          p(null, "Digest: ", code({ "data-testid": "manifest-digest" }, ctx.importDigest)),
          h("details", null, h("summary", null, "Canonical text (digested input)"), codeBlock(ctx.importCanonical ?? "", "manifest-canonical")),
        )
      : null,
    p(null, navLink(ctx.navigate, "/guide/2-qualification", "Continue to qualification →")),
  );
}
