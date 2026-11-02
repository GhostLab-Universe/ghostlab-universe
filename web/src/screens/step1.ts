/* Step 1: select an environment and import its manifest. */

import { useEffect, useState, type ReactNode } from "react";
import { ILLUSTRATIVE_MANIFEST_TEXT } from "../fixtures";
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

  useEffect(() => {
    let cancelled = false;
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
  }, [ctx.api]);

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

  return div(
    null,
    h1(null, "Step 1: Select environment & import manifest"),
    banner("info", "Fixture mode", "Environment data below is an illustrative dev aid until the Track A service lands."),
    loadError !== null
      ? banner("error", "Could not list environments", loadError)
      : envs === null
        ? p(null, "Loading environments…")
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
          ),
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
