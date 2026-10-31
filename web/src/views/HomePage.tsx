import { useState } from "react";
import type { SessionInfo, WorldCampaignSpec } from "../api/api.generated.ts";
import { OPERATIONS } from "../api/operations.generated.ts";
import type { FixtureHarness } from "../data/fixtureSource.ts";
import type { DataSource, Failure } from "../data/source.ts";
import { campaignHref } from "../logic/routes.ts";
import { FailureNotice, Notice, Section } from "../ui/components.tsx";

const ID_PATTERN = "[A-Za-z0-9][A-Za-z0-9._:\\-]{0,127}";

/** M00's published local-demo limits. The service still applies its own ceilings. */
const LOCAL_DEMO_BUDGET: WorldCampaignSpec["budget"] = {
  cost_currency: null,
  max_cost_minor: null,
  max_input_tokens: 200000,
  max_model_calls: 20,
  max_output_tokens: 20000,
  max_parallel_runs: 2,
  max_tool_calls: 60,
  max_wall_clock_ms: 600000,
};

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
        <h1>Experiments on synthetic worlds</h1>
        <p className="lead">
          Compare a baseline with a faulted fork, inspect the evidence, replay the failure and decide on the repair.
          Verdicts come from the verifier. Worlds and money here are synthetic.
        </p>
      </header>
      {harness === null ? null : <ScenarioList harness={harness} />}
      <OpenCampaign />
      {source.kind === "api" && session !== null ? <StartCampaign source={source} session={session} /> : null}
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
  return (
    <Section title="Open a campaign">
      <p className="muted">The service has no campaign list, so a campaign is opened by its id.</p>
      <form
        className="inline-form"
        onSubmit={(event) => {
          event.preventDefault();
          window.location.hash = campaignHref(id.trim());
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
  const [key] = useState(() => `campaign-${crypto.randomUUID()}`);
  const scenarios = session.inference === "scripted" ? OPERATIONS.createCampaign.queryValues.scripted_scenario : null;
  const [scenario, setScenario] = useState("default");

  if (worlds.length === 0) {
    return (
      <Section title="Start a campaign">
        <Notice state="empty" title="No runnable worlds on this service" />
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
      budget: LOCAL_DEMO_BUDGET,
    };
    const result = await source.createCampaign(spec, key, scenarios === null ? undefined : scenario);
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
        <select id="world" value={world} onChange={(event) => setWorld(event.currentTarget.value as typeof world)}>
          {worlds.map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </select>
        {scenarios === null ? null : (
          <>
            <label htmlFor="scenario">Scripted scenario (checked-in proposals, not a model)</label>
            <select id="scenario" value={scenario} onChange={(event) => setScenario(event.currentTarget.value)}>
              {scenarios.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </>
        )}
        <label htmlFor="objective">Objective</label>
        <input id="objective" value={objective} required maxLength={2000} onChange={(event) => setObjective(event.currentTarget.value)} />
        <label htmlFor="criterion">Success criterion</label>
        <input id="criterion" value={criterion} required maxLength={2000} onChange={(event) => setCriterion(event.currentTarget.value)} />
        <button type="submit" disabled={busy}>
          {busy ? "Starting" : "Start campaign"}
        </button>
      </form>
      {failure === null ? null : <FailureNotice failure={failure} />}
    </Section>
  );
}
