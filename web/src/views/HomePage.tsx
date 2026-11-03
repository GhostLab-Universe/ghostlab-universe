import { useRef, useState } from "react";
import type { SessionInfo, WorldCampaignSpec } from "../api/api.generated.ts";
import { OPERATIONS } from "../api/operations.generated.ts";
import type { FixtureHarness } from "../data/fixtureSource.ts";
import type { DataSource, Failure } from "../data/source.ts";
import { IdempotencyKeys } from "../logic/approval.ts";
import {
  GUIDED_DEMO_BUDGET,
  GUIDED_DEMO_CRITERION,
  GUIDED_DEMO_OBJECTIVE,
  GUIDED_DEMO_SCENARIO,
  GUIDED_DEMO_STEPS,
  GUIDED_DEMO_WORLD,
  guidedDemoAvailable,
  guidedDemoInferenceNote,
  guidedDemoSpec,
} from "../logic/guidedDemo.ts";
import { campaignHref } from "../logic/routes.ts";
import { BusyNote, Facts, FailureNotice, Mono, Notice, Section } from "../ui/components.tsx";

const ID_PATTERN = "[A-Za-z0-9][A-Za-z0-9._:\\-]{0,127}";

const RECENTS_KEY = "ghostlab.recent-campaigns.v1";
const MAX_RECENTS = 8;

/** Campaign ids opened from this browser. A convenience list, never service data. */
function readRecents(): string[] {
  try {
    if (typeof localStorage === "undefined") return [];
    const raw = localStorage.getItem(RECENTS_KEY);
    if (raw === null) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item): item is string => typeof item === "string").slice(0, MAX_RECENTS);
  } catch {
    return [];
  }
}

function rememberCampaign(id: string): string[] {
  const next = [id, ...readRecents().filter((item) => item !== id)].slice(0, MAX_RECENTS);
  try {
    if (typeof localStorage !== "undefined") localStorage.setItem(RECENTS_KEY, JSON.stringify(next));
  } catch {
    // Private browsing or quota: recents are a convenience, not data.
  }
  return next;
}

export function HomePage({
  source,
  harness,
  session,
}: {
  source: DataSource;
  harness: FixtureHarness | null;
  session: SessionInfo | null;
}) {
  return (
    <>
      <header className="campaign-head">
        <h1>Find the failure. Prove the fix.</h1>
        <p className="lead">
          Test an agent under pressure. Compare what changed, follow the evidence, and verify a repair in a repeatable test environment.
        </p>
      </header>
      {harness === null ? null : <ScenarioList harness={harness} />}
      <div className="home-workspace"><div className="home-primary"><a className="connect-project" href="#/extensions"><strong>Connect a project</strong><span>Inspect a GitHub repository or choose a test environment.</span><span aria-hidden="true">＋</span></a>
      {source.kind === "api" && session !== null ? <GuidedDemo source={source} session={session} /> : null}
      {source.kind === "api" && session !== null ? <StartCampaign source={source} session={session} /> : null}
      </div><aside><OpenCampaign /><div className="workflow-note"><h2>How a test works</h2><ol><li>Choose an environment and agent.</li><li>Introduce a controlled fault.</li><li>Compare execution evidence.</li><li>Review and verify the repair.</li></ol><p>Built-in environments use synthetic data. Findings come from executed checks.</p></div></aside></div>
    </>
  );
}

function ScenarioList({ harness }: { harness: FixtureHarness }) {
  const groups = [
    { name: "flow", title: "Complete flows" },
    { name: "state", title: "Interface states" },
  ] as const;
  return (
    <>
      {groups.map((group) => (
        <Section key={group.name} title={group.title}>
          <ul className="scenarios">
            {harness.scenarios
              .filter((scenario) => scenario.group === group.name)
              .map((scenario) => (
                <li key={scenario.id}>
                  <a href={campaignHref(scenario.id)}>{scenario.title}</a>
                  <p>{scenario.shows}</p>
                </li>
              ))}
          </ul>
        </Section>
      ))}
    </>
  );
}

function OpenCampaign() {
  const [id, setId] = useState("");
  const [recents, setRecents] = useState(readRecents);
  return (
    <Section title="Open a campaign">
      <p className="muted">Continue a previous run using its campaign ID.</p>
      <form
        className="inline-form"
        onSubmit={(event) => {
          event.preventDefault();
          const trimmed = id.trim();
          setRecents(rememberCampaign(trimmed));
          window.location.hash = campaignHref(trimmed);
        }}
      >
        <label htmlFor="campaign-id">Campaign id</label>
        <input
          id="campaign-id"
          value={id}
          required
          pattern={ID_PATTERN}
          maxLength={128}
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => setId(event.currentTarget.value)}
        />
        <button type="submit">Open</button>
      </form>
      {recents.length === 0 ? null : (
        <>
          <h3>Recently opened in this browser</h3>
          <ul className="recents">
            {recents.map((item) => (
              <li key={item}>
                <a href={campaignHref(item)}>
                  <Mono>{item}</Mono>
                </a>
              </li>
            ))}
          </ul>
        </>
      )}
    </Section>
  );
}

/** One click runs the scripted refund-world scenario, start to verdict, on synthetic data. */
export function GuidedDemo({ source, session }: { source: DataSource; session: SessionInfo }) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const keys = useRef(new IdempotencyKeys(() => `campaign-${crypto.randomUUID()}`));
  if (!guidedDemoAvailable(session.runnable_worlds as readonly string[])) return null;
  const disabled = session.inference === "disabled";
  const scripted = session.inference === "scripted" || session.inference === "fixture";

  async function start() {
    setBusy(true);
    setFailure(null);
    const spec = guidedDemoSpec();
    const chosen = session.inference === "scripted" ? GUIDED_DEMO_SCENARIO : undefined;
    const result = await source.createCampaign(spec, keys.current.keyFor({ spec, chosen }), chosen);
    setBusy(false);
    if (result.ok) window.location.hash = campaignHref(result.data.record.campaign_id);
    else setFailure(result.failure);
  }

  return (
    <Section title="Guided demo run">
      <div className="guided-demo" data-testid="guided-demo">
        <p>
          Run the refund example, inspect its evidence, and review the repair decision. <span data-testid="guided-demo-synthetic">Synthetic world: no real people or money.</span>
        </p>
        <ol className="steps" data-testid="guided-demo-steps">
          {GUIDED_DEMO_STEPS.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
        <Facts
          facts={[
            { term: "World", detail: <Mono>{GUIDED_DEMO_WORLD}</Mono> },
            { term: "Objective", detail: GUIDED_DEMO_OBJECTIVE },
            { term: "Success criterion", detail: GUIDED_DEMO_CRITERION },
            { term: "Inference", detail: guidedDemoInferenceNote(session.inference) },
            ...(scripted
              ? [{ term: "Scripted scenario", detail: <Mono>{GUIDED_DEMO_SCENARIO}</Mono> }]
              : []),
          ]}
        />
        {disabled ? (
          <Notice state="empty" title="Inference is disabled on this service">
            It cannot start new campaigns.
          </Notice>
        ) : null}
        <div className="actions">
          <button type="button" disabled={busy || disabled} aria-busy={busy} onClick={() => void start()}>
            {busy ? "Starting" : "Start guided demo run"}
          </button>
        </div>
        {busy ? <BusyNote>Starting the guided demo run.</BusyNote> : null}
        {failure === null ? null : <FailureNotice failure={failure} />}
      </div>
    </Section>
  );
}

function StartCampaign({ source, session }: { source: DataSource; session: SessionInfo }) {
  const worlds = session.runnable_worlds as readonly WorldCampaignSpec["world_version"][];
  const [world, setWorld] = useState(worlds[0] ?? "refund-world/1");
  const [objective, setObjective] = useState("");
  const [criterion, setCriterion] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const keys = useRef(new IdempotencyKeys(() => `campaign-${crypto.randomUUID()}`));
  const scenarios = session.inference === "scripted" ? OPERATIONS.createCampaign.queryValues.scripted_scenario : null;
  const [scenario, setScenario] = useState("default");

  if (worlds.length === 0) {
    return (
      <Section title="Start a campaign">
        <Notice state="empty" title="No runnable worlds on this service">
          The service lists no worlds this session can start.
        </Notice>
      </Section>
    );
  }

  async function start() {
    setBusy(true);
    setFailure(null);
    const spec: WorldCampaignSpec = {
      schema_version: 1,
      world_version: world,
      objective: objective.trim(),
      success_criteria: [criterion.trim()],
      budget: GUIDED_DEMO_BUDGET,
    };
    const chosen = scenarios === null ? undefined : scenario;
    const result = await source.createCampaign(spec, keys.current.keyFor({ spec, chosen }), chosen);
    setBusy(false);
    if (result.ok) window.location.hash = campaignHref(result.data.record.campaign_id);
    else setFailure(result.failure);
  }

  return (
    <Section title="Start a campaign">
      <form
        className="stack-form"
        onSubmit={(event) => {
          event.preventDefault();
          void start();
        }}
      >
        <label htmlFor="world">World</label>
        <select id="world" value={world} disabled={busy} onChange={(event) => setWorld(event.currentTarget.value as typeof world)}>
          {worlds.map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </select>
        {scenarios === null ? null : (
          <>
            <label htmlFor="scenario">Scripted scenario (checked-in proposals, not a model)</label>
            <select id="scenario" value={scenario} disabled={busy} onChange={(event) => setScenario(event.currentTarget.value)}>
              {scenarios.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </>
        )}
        <label htmlFor="objective">Objective</label>
        <input id="objective" value={objective} required maxLength={2000} disabled={busy} onChange={(event) => setObjective(event.currentTarget.value)} />
        <label htmlFor="criterion">Success criterion</label>
        <input id="criterion" value={criterion} required maxLength={2000} disabled={busy} onChange={(event) => setCriterion(event.currentTarget.value)} />
        <button type="submit" disabled={busy}>
          {busy ? "Starting" : "Start campaign"}
        </button>
      </form>
      {failure === null ? null : <FailureNotice failure={failure} />}
    </Section>
  );
}
