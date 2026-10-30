import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createApiClient } from "./api/client.ts";
import type { DataSource } from "./data/source.ts";
import type { FixtureHarness } from "./data/fixtureSource.ts";
import "./styles.css";
import { App } from "./ui/App.tsx";

async function chooseSource(): Promise<{ source: DataSource; harness: FixtureHarness | null }> {
  // MODE is replaced at build time, so a normal build contains no fixture code or data.
  if (import.meta.env.MODE === "fixture") {
    const { createFixtureSource } = await import("./data/fixtureSource.ts");
    const source = createFixtureSource();
    return { source, harness: source.harness };
  }
  return { source: createApiClient(), harness: null };
}

const { source, harness } = await chooseSource();
const root = document.getElementById("root");
if (root !== null) {
  createRoot(root).render(
    <StrictMode>
      <App source={source} harness={harness} />
    </StrictMode>,
  );
}
