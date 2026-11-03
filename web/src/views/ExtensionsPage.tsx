import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import type {
  ExpDiff,
  ExpPolicy,
  ExpRequested,
  ExpResolved,
  ExpRun,
  ExtCampaignReport,
  ExtEnvironmentDetail,
  ExtEnvironmentList,
  ExtFidelityReport,
  ExtFixture,
  ExtInspection,
  ExtMinimizeResult,
  ExtQualificationReport,
  ExtReplayResult,
  ExtRetestResult,
} from "../api/api.generated.ts";
import type { DataSource, Failure, Result } from "../data/source.ts";
import { Facts, FailureNotice, JsonBlock, Loading, Mono, Section, Tag } from "../ui/components.tsx";

function downloadJson(filename: string, value: unknown) {
  const blob = new Blob([JSON.stringify(value, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function parseJson(text: string): { value?: unknown; error?: string } {
  try {
    return { value: JSON.parse(text) as unknown };
  } catch (error) {
    return { error: error instanceof Error ? error.message : "Invalid JSON" };
  }
}

function JsonArea({ label, value, onChange }: { label: string; value: string; onChange: (text: string) => void }) {
  return (
    <label className="field">
      <span>{label}</span>
      <textarea rows={6} spellCheck={false} value={value} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}

function TextInput({ label, value, onChange }: { label: string; value: string; onChange: (text: string) => void }) {
  return (
    <label className="field">
      <span>{label}</span>
      <input type="text" value={value} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}

function Outcome<T>({ result, render, what }: { result: Result<T> | null; render: (data: T) => ReactNode; what?: string }) {
  // Reads that start with the page name what they load; action results stay
  // blank until the action runs.
  if (result === null) return what === undefined ? null : <Loading what={what} />;
  if (!result.ok) return <FailureNotice failure={result.failure} />;
  return <>{render(result.data)}</>;
}

function defaultScenario(world: string): string {
  if (world.includes("reservation")) {
    return JSON.stringify({ timeout_after_commit: false, stale_snapshot: false, skip_capacity_check: false, max_tool_calls: 20 }, null, 2);
  }
  return JSON.stringify({ timeout_after_commit: false, max_tool_calls: 20 }, null, 2);
}

function defaultTaskParams(world: string, snapshot: Record<string, unknown> | null): Record<string, string> {
  if (world.includes("reservation")) return {};
  const orderId = typeof snapshot?.order_id === "string" ? snapshot.order_id : "order-1";
  const customerId = typeof snapshot?.customer_id === "string" ? snapshot.customer_id : "customer-1";
  return { order_id: orderId, customer_id: customerId };
}

function defaultScript(world: string, snapshot: Record<string, unknown> | null): string {
  if (world.includes("reservation")) {
    return JSON.stringify([{ tool: "check_availability", arguments: {} }], null, 2);
  }
  const orderId = typeof snapshot?.order_id === "string" ? snapshot.order_id : "order-1";
  const customerId = typeof snapshot?.customer_id === "string" ? snapshot.customer_id : "customer-1";
  return JSON.stringify([{ tool: "get_order", arguments: { order_id: orderId, customer_id: customerId } }], null, 2);
}

export function ExtensionsPage({ source }: { source: DataSource }) {
  const [envs, setEnvs] = useState<Result<ExtEnvironmentList> | null>(null);
  const [world, setWorld] = useState<string>("");
  const [detail, setDetail] = useState<Result<ExtEnvironmentDetail> | null>(null);

  useEffect(() => {
    let cancelled = false;
    source.listExtEnvironments().then((result) => {
      if (cancelled) return;
      setEnvs(result);
      if (result.ok && result.data.environments.length > 0 && world === "") {
        setWorld(result.data.environments[0]?.world_version ?? "");
      }
    });
    return () => {
      cancelled = true;
    };
  }, [source, world]);

  useEffect(() => {
    if (world === "") return;
    let cancelled = false;
    setDetail(null);
    source.getExtEnvironment(world).then((result) => {
      if (!cancelled) setDetail(result);
    });
    return () => {
      cancelled = true;
    };
  }, [source, world]);

  return (
    <>
      <h1>Extensions</h1>
      <p className="muted">Reviewed local environments and target agents. Worlds here are synthetic test fixtures.</p>
      <Outcome
        result={envs}
        what="environments"
        render={(data) => (
          <Section title="Connected environments">
            <Facts
              facts={data.environments.map((env) => ({
                term: env.world_version,
                detail: (
                  <>
                    <Mono>{env.environment_id}</Mono> <Mono>{env.environment_version}</Mono>{" "}
                    <button type="button" onClick={() => setWorld(env.world_version)} disabled={env.world_version === world}>
                      {env.world_version === world ? "Selected" : "Select"}
                    </button>{" "}
                    <span className="muted">
                      {env.tools.length} tools, {env.faults.length} faults, {env.fixtures.length} fixtures,{" "}
                      {env.capabilities.snapshot_restore ? "snapshots" : "no snapshots"}, targets: {env.targets.join(", ") || "none"}
                    </span>
                  </>
                ),
              }))}
            />
          </Section>
        )}
      />
      {world === "" ? null : (
        <Outcome
          result={detail}
          what="environment"
          render={(data) => <EnvironmentDetail source={source} detail={data} />}
        />
      )}
    </>
  );
}

function EnvironmentDetail({ source, detail }: { source: DataSource; detail: ExtEnvironmentDetail }) {
  const [fixtureName, setFixtureName] = useState<string>(detail.fixtures[0] ?? "");
  const [fixture, setFixture] = useState<Result<ExtFixture> | null>(null);

  useEffect(() => {
    if (fixtureName === "") return;
    let cancelled = false;
    setFixture(null);
    source.getExtFixture(detail.world_version, fixtureName).then((result) => {
      if (!cancelled) setFixture(result);
    });
    return () => {
      cancelled = true;
    };
  }, [source, detail.world_version, fixtureName]);

  const snapshot = fixture !== null && fixture.ok ? (fixture.data.snapshot as Record<string, unknown>) : null;
  return (
    <>
      <Section title={detail.world_version}>
        <Facts
          facts={[
            { term: "Environment", detail: <><Mono>{detail.environment_id}</Mono> <Mono>{detail.environment_version}</Mono></> },
            { term: "Source", detail: <Mono>{detail.source}</Mono> },
            { term: "Package", detail: <><Mono>{detail.package}</Mono> <Mono>{detail.package_version}</Mono></> },
            { term: "Tools", detail: <span>{detail.tools.map((tool) => <Mono key={tool}>{tool} </Mono>)}</span> },
            { term: "Faults", detail: <span>{detail.faults.map((fault) => <Mono key={fault}>{fault} </Mono>)}</span> },
            { term: "Invariants", detail: <span>{detail.invariants.map((name) => <Mono key={name}>{name} </Mono>)}</span> },
            { term: "Targets", detail: <span>{detail.targets.length > 0 ? detail.targets.map((id) => <Mono key={id}>{id} </Mono>) : "none registered"}</span> },
            {
              term: "Capabilities",
              detail: (
                <span>
                  {Object.entries(detail.capabilities).map(([name, have]) => (
                    <span key={name}><Tag tone={have ? "ok" : "neutral"}>{name}</Tag> </span>
                  ))}
                </span>
              ),
            },
          ]}
        />
        <label className="field">
          <span>Fixture</span>
          <select value={fixtureName} onChange={(event) => setFixtureName(event.target.value)}>
            {detail.fixtures.map((name) => (
              <option key={name} value={name}>{name}</option>
            ))}
          </select>
        </label>
        <Outcome result={fixture} what="fixture" render={(data) => <JsonBlock label={`Fixture ${data.name}`} value={data.snapshot} />} />
      </Section>
      <QualifyPanel source={source} detail={detail} fixtureName={fixtureName} snapshot={snapshot} />
      <CampaignPanel source={source} detail={detail} fixtureName={fixtureName} snapshot={snapshot} />
      <InspectPanel source={source} />
      <ReplayPanel source={source} detail={detail} />
      <RetestPanel source={source} detail={detail} fixtureName={fixtureName} snapshot={snapshot} />
      <MinimizePanel source={source} detail={detail} fixtureName={fixtureName} snapshot={snapshot} />
      <ExperimentPanel source={source} detail={detail} fixtureName={fixtureName} snapshot={snapshot} />
    </>
  );
}

function QualifyPanel({ source, detail, fixtureName, snapshot }: { source: DataSource; detail: ExtEnvironmentDetail; fixtureName: string; snapshot: Record<string, unknown> | null }) {
  const [scenario, setScenario] = useState<string>(() => defaultScenario(detail.world_version));
  const [script, setScript] = useState<string>(() => defaultScript(detail.world_version, snapshot));
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Result<ExtQualificationReport> | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);

  async function run(kind: "qualify" | "fidelity") {
    const parsedScenario = parseJson(scenario);
    const parsedScript = parseJson(script);
    if (parsedScenario.error !== undefined || parsedScript.error !== undefined) {
      setFailure({ kind: "transport", message: parsedScenario.error ?? parsedScript.error ?? "Invalid JSON" });
      return;
    }
    setFailure(null);
    setBusy(true);
    try {
      if (kind === "qualify") {
        setResult(await source.qualifyExtension({ world_version: detail.world_version, fixture: fixtureName, scenario: parsedScenario.value as Record<string, never>, script: parsedScript.value as never, task_id: "console-probe" }));
      } else {
        const actor = typeof snapshot?.customer_id === "string" ? snapshot.customer_id : "console-probe";
        const fidelity = await source.assessExtFidelity({ world_version: detail.world_version, fixture: fixtureName, scenario: parsedScenario.value as Record<string, never>, actor_id: actor, script: parsedScript.value as never, repeats: 2 });
        const asQualify: ExtQualificationReport = {
          world_version: fidelity.ok ? fidelity.data.world_version : detail.world_version,
          environment_id: detail.environment_id,
          passed: fidelity.ok ? fidelity.data.passed : false,
          checks: [{ name: "fidelity", passed: fidelity.ok ? fidelity.data.passed : false, details: fidelity.ok ? `determinism=${fidelity.data.determinism_identical} replay=${fidelity.data.replay_reproduced}` : "failed" }],
          missing_capabilities: fidelity.ok ? fidelity.data.missing_capabilities : [],
          notes: fidelity.ok ? fidelity.data.notes : [],
          details: (fidelity.ok ? fidelity.data.details : {}) as Record<string, never>,
        };
        setResult(fidelity.ok ? { ok: true, data: asQualify, origins: fidelity.origins } : { ok: false, failure: fidelity.failure });
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section title="Qualification and fidelity">
      <TextInput label="Fixture" value={fixtureName} onChange={() => undefined} />
      <JsonArea label="Scenario" value={scenario} onChange={setScenario} />
      <JsonArea label="Probe script" value={script} onChange={setScript} />
      <p>
        <button type="button" disabled={busy} onClick={() => void run("qualify")}>{busy ? "Running…" : "Run qualification"}</button>{" "}
        <button type="button" disabled={busy} onClick={() => void run("fidelity")}>{busy ? "Running…" : "Run fidelity"}</button>
      </p>
      {failure !== null ? <FailureNotice failure={failure} /> : null}
      <Outcome
        result={result}
        render={(data) => (
          <>
            <p><Tag tone={data.passed ? "ok" : "fail"}>{data.passed ? "Pass" : "Fail"}</Tag></p>
            <Facts facts={data.checks.map((check) => ({ term: check.name, detail: <><Tag tone={check.passed ? "ok" : "fail"}>{check.passed ? "pass" : "fail"}</Tag> <span className="muted">{check.details}</span></> }))} />
            {data.missing_capabilities.length > 0 ? <p className="muted">Missing: {data.missing_capabilities.join(", ")}</p> : null}
            <JsonBlock label="Full report" value={data.details} />
          </>
        )}
      />
    </Section>
  );
}

function CampaignPanel({ source, detail, fixtureName, snapshot }: { source: DataSource; detail: ExtEnvironmentDetail; fixtureName: string; snapshot: Record<string, unknown> | null }) {
  const customerId = typeof snapshot?.customer_id === "string" ? snapshot.customer_id : "console-probe";
  const [attempts, setAttempts] = useState<string>(() =>
    JSON.stringify([{
      name: "console-1", arm: "fixed-scenario", fixture_name: fixtureName,
      scenario: JSON.parse(defaultScenario(detail.world_version)) as unknown,
      actor_id: customerId, task: { task_id: "console", params: defaultTaskParams(detail.world_version, snapshot) },
      target: detail.targets.length > 0 ? { kind: "registered", id: detail.targets[0] } : { kind: "script", actions: JSON.parse(defaultScript(detail.world_version, snapshot)) as unknown },
      cap: 20,
    }], null, 2),
  );
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Result<ExtCampaignReport> | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);

  async function run() {
    const parsed = parseJson(attempts);
    if (parsed.error !== undefined) {
      setFailure({ kind: "transport", message: parsed.error });
      return;
    }
    setFailure(null);
    setBusy(true);
    try {
      setResult(await source.runExtCampaign({ world_version: detail.world_version, attempts: parsed.value as never }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section title="Campaign">
      <JsonArea label="Attempts" value={attempts} onChange={setAttempts} />
      <p><button type="button" disabled={busy} onClick={() => void run()}>{busy ? "Running…" : "Run campaign"}</button></p>
      {failure !== null ? <FailureNotice failure={failure} /> : null}
      <Outcome
        result={result}
        render={(data) => (
          <>
            <Facts
              facts={[
                { term: "Attempts", detail: String(data.attempts_run) },
                { term: "Tools used", detail: data.tools_used.join(", ") || "none" },
                { term: "Faults fired", detail: data.faults_fired.join(", ") || "none" },
                { term: "Rules failed", detail: data.rules_failed.length > 0 ? <><Tag tone="fail">violations</Tag> {data.rules_failed.join(", ")}</> : "none" },
                { term: "Budgets", detail: data.caps_respected ? "respected" : "exceeded" },
              ]}
            />
            <JsonBlock label="Full report" value={data.details} />
          </>
        )}
      />
    </Section>
  );
}

function InspectPanel({ source }: { source: DataSource }) {
  const [envelope, setEnvelope] = useState("{}");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Result<ExtInspection> | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);

  async function run() {
    const parsed = parseJson(envelope);
    if (parsed.error !== undefined) {
      setFailure({ kind: "transport", message: parsed.error });
      return;
    }
    setFailure(null);
    setBusy(true);
    try {
      setResult(await source.inspectExtCapsule({ envelope: parsed.value as Record<string, never> }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section title="Inspect a capsule">
      <JsonArea label="Envelope JSON" value={envelope} onChange={setEnvelope} />
      <p><button type="button" disabled={busy} onClick={() => void run()}>{busy ? "Running…" : "Inspect"}</button></p>
      {failure !== null ? <FailureNotice failure={failure} /> : null}
      <Outcome
        result={result}
        render={(data) => (
          <>
            <p>
              <Tag tone={data.replay_supported ? "ok" : "warn"}>{data.replay_supported ? "recorded replay" : "no recorded replay"}</Tag>{" "}
              <Tag tone="neutral">retest: new experiment</Tag>{" "}
              <span className="muted">{data.replay_detail}</span>
            </p>
            <EnforcementOutcome inspection={data} />
            <Facts
              facts={[
                { term: "Schema", detail: <Mono>v{data.schema_version}</Mono> },
                { term: "World", detail: <Mono>{data.world_version}</Mono> },
                { term: "Kind", detail: <Mono>{data.replay_kind}</Mono> },
                { term: "Failed", detail: data.failed_invariants.join(", ") || "none" },
                { term: "External", detail: data.external.join(", ") || "none" },
              ]}
            />
            <JsonBlock label="Full inspection" value={data.details} />
          </>
        )}
      />
    </Section>
  );
}

interface InspectionMode {
  supported: boolean;
  detail: string;
}

function readMode(value: unknown): InspectionMode | null {
  if (typeof value !== "object" || value === null) return null;
  const mode = value as Record<string, unknown>;
  if (typeof mode.supported !== "boolean" || typeof mode.detail !== "string") return null;
  return { supported: mode.supported, detail: mode.detail };
}

/**
 * The enforcement outcome beside replay and retest, read from the
 * inspection's own details: the `enforced_execution` mode plus the
 * recorded failed invariants. No extra request.
 */
export function EnforcementOutcome({ inspection }: { inspection: ExtInspection }) {
  const mode = readMode(inspection.details["enforced_execution"]);
  if (mode === null) {
    return (
      <p className="muted" data-testid="enforcement-outcome">
        This inspection carries no enforcement mode.
      </p>
    );
  }
  if (!mode.supported) {
    return (
      <p data-testid="enforcement-outcome">
        <Tag tone="neutral">no enforcement outcome</Tag> <span className="muted">{mode.detail}</span>
      </p>
    );
  }
  const failed = inspection.failed_invariants;
  return (
    <div data-testid="enforcement-outcome">
      <p>
        <Tag tone={failed.length > 0 ? "fail" : "ok"}>{failed.length > 0 ? "Enforcement violated" : "Enforcement held"}</Tag>{" "}
        <span className="muted">{mode.detail}</span>
      </p>
      {failed.length > 0 ? <Facts facts={[{ term: "Failed", detail: failed.join(", ") }]} /> : null}
    </div>
  );
}

function ReplayPanel({ source, detail }: { source: DataSource; detail: ExtEnvironmentDetail }) {
  const [evidence, setEvidence] = useState('{\n  "fixture": {},\n  "scenario": {},\n  "actor_id": "",\n  "trace": [],\n  "state": {},\n  "checks": {}\n}');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Result<ExtReplayResult> | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);

  async function run() {
    const parsed = parseJson(evidence);
    if (parsed.error !== undefined || typeof parsed.value !== "object" || parsed.value === null) {
      setFailure({ kind: "transport", message: parsed.error ?? "Need an evidence object" });
      return;
    }
    setFailure(null);
    setBusy(true);
    try {
      const bundle = parsed.value as Record<string, unknown>;
      setResult(await source.replayExtRecorded({ world_version: detail.world_version, fixture: bundle.fixture as { [key: string]: unknown }, scenario: bundle.scenario as { [key: string]: unknown }, actor_id: bundle.actor_id as string, trace: bundle.trace as unknown[], state: bundle.state as { [key: string]: unknown }, checks: bundle.checks as { [key: string]: boolean } }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section title="Recorded replay">
      <JsonArea label="Recorded evidence bundle" value={evidence} onChange={setEvidence} />
      <p><button type="button" disabled={busy} onClick={() => void run()}>{busy ? "Running…" : "Replay"}</button></p>
      {failure !== null ? <FailureNotice failure={failure} /> : null}
      <Outcome
        result={result}
        render={(data) => (
          <>
            <p><Tag tone={data.reproduced ? "ok" : "fail"}>{data.reproduced ? "reproduced" : "not reproduced"}</Tag></p>
            <Facts
              facts={[
                { term: "Trace", detail: String(data.matches.trace) },
                { term: "Final state", detail: String(data.matches.final_state) },
                { term: "Checks", detail: String(data.matches.checks) },
                { term: "Failed", detail: data.failed.join(", ") || "none" },
              ]}
            />
            <JsonBlock label="Full result" value={data.details} />
          </>
        )}
      />
    </Section>
  );
}

function RetestPanel({ source, detail, fixtureName, snapshot }: { source: DataSource; detail: ExtEnvironmentDetail; fixtureName: string; snapshot: Record<string, unknown> | null }) {
  const [targetId, setTargetId] = useState(detail.targets[0] ?? "");
  const [script, setScript] = useState(() => defaultScript(detail.world_version, snapshot));
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Result<ExtRetestResult> | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);

  async function run() {
    const customerId = typeof snapshot?.customer_id === "string" ? snapshot.customer_id : "console-probe";
    const parsed = parseJson(script);
    if (targetId === "" && parsed.error !== undefined) {
      setFailure({ kind: "transport", message: parsed.error ?? "Invalid script" });
      return;
    }
    setFailure(null);
    setBusy(true);
    try {
      const target = targetId !== "" ? { kind: "registered", id: targetId } : { kind: "script", actions: parsed.value };
      const fixtureSnap = await source.getExtFixture(detail.world_version, fixtureName);
      if (!fixtureSnap.ok) {
        setResult({ ok: false, failure: fixtureSnap.failure });
        return;
      }
      setResult(await source.retestExtFresh({
        world_version: detail.world_version, target: target as never, fixture: fixtureSnap.data.snapshot as Record<string, never>,
        scenario: JSON.parse(defaultScenario(detail.world_version)) as Record<string, never>, actor_id: customerId,
        task: { task_id: "console", params: defaultTaskParams(detail.world_version, snapshot) }, cap: 20,
      }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section title="Fresh retest">
      <label className="field">
        <span>Target (blank runs the script below)</span>
        <select value={targetId} onChange={(event) => setTargetId(event.target.value)}>
          <option value="">Inline script</option>
          {detail.targets.map((id) => <option key={id} value={id}>{id}</option>)}
        </select>
      </label>
      {targetId === "" ? <JsonArea label="Script" value={script} onChange={setScript} /> : null}
      <p><button type="button" disabled={busy} onClick={() => void run()}>{busy ? "Running…" : "Retest"}</button></p>
      {failure !== null ? <FailureNotice failure={failure} /> : null}
      <Outcome
        result={result}
        render={(data) => (
          <>
            <p>
              <Tag tone={data.failed.length > 0 ? "fail" : "ok"}>{data.failed.length > 0 ? "failed" : "passed"}</Tag>{" "}
              <Tag tone="neutral">new experiment, never replay equivalence</Tag>{" "}
              {data.denied ? <Tag tone="warn">denied</Tag> : null}
            </p>
            <Facts
              facts={[
                { term: "Target", detail: <><Mono>{data.target_adapter_id}</Mono> <Mono>{data.target_adapter_version}</Mono></> },
                { term: "Status", detail: <Mono>{data.status}</Mono> },
                { term: "Failed", detail: data.failed.join(", ") || "none" },
              ]}
            />
            <JsonBlock label="Full result" value={data.details} />
          </>
        )}
      />
    </Section>
  );
}

function MinimizePanel({ source, detail, fixtureName, snapshot }: { source: DataSource; detail: ExtEnvironmentDetail; fixtureName: string; snapshot: Record<string, unknown> | null }) {
  const [targetId, setTargetId] = useState(detail.targets[0] ?? "");
  const [invariant, setInvariant] = useState(detail.invariants[0] ?? "");
  const [faultKeys, setFaultKeys] = useState(detail.faults.join(", "));
  const [template, setTemplate] = useState('{\n  "max_tool_calls": 20\n}');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Result<ExtMinimizeResult> | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);

  async function run() {
    const parsed = parseJson(template);
    if (parsed.error !== undefined) {
      setFailure({ kind: "transport", message: parsed.error });
      return;
    }
    if (targetId === "" || invariant === "") {
      setFailure({ kind: "transport", message: "Pick a target and an invariant" });
      return;
    }
    setFailure(null);
    setBusy(true);
    try {
      const customerId = typeof snapshot?.customer_id === "string" ? snapshot.customer_id : "console-probe";
      const fixtureSnap = await source.getExtFixture(detail.world_version, fixtureName);
      if (!fixtureSnap.ok) {
        setResult({ ok: false, failure: fixtureSnap.failure });
        return;
      }
      setResult(await source.minimizeExtFaults({
        world_version: detail.world_version, target: { kind: "registered", id: targetId } as never,
        fixture: fixtureSnap.data.snapshot as Record<string, never>, scenario_template: parsed.value as Record<string, never>,
        fault_keys: faultKeys.split(",").map((key) => key.trim()).filter((key) => key !== "") as never, actor_id: customerId,
        task: { task_id: "console", params: defaultTaskParams(detail.world_version, snapshot) }, cap: 20, invariant,
      }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section title="Minimize and regression export">
      <label className="field">
        <span>Target</span>
        <select value={targetId} onChange={(event) => setTargetId(event.target.value)}>
          {detail.targets.map((id) => <option key={id} value={id}>{id}</option>)}
        </select>
      </label>
      <label className="field">
        <span>Invariant</span>
        <select value={invariant} onChange={(event) => setInvariant(event.target.value)}>
          {detail.invariants.map((name) => <option key={name} value={name}>{name}</option>)}
        </select>
      </label>
      <TextInput label="Fault keys (comma separated)" value={faultKeys} onChange={setFaultKeys} />
      <JsonArea label="Scenario template" value={template} onChange={setTemplate} />
      <p><button type="button" disabled={busy} onClick={() => void run()}>{busy ? "Running…" : "Minimize"}</button></p>
      {failure !== null ? <FailureNotice failure={failure} /> : null}
      <Outcome
        result={result}
        render={(data) => (
          <>
            <p>
              <Tag tone={data.status === "reduced" || data.status === "already_minimal" ? "ok" : "warn"}>{data.status}</Tag>{" "}
              <span className="muted">selected: {(data.selected_faults ?? []).join(", ") || "none"}; removed: {data.removed_faults.join(", ") || "none"}; runs: {data.runs_used}</span>
            </p>
            <p>
              <button type="button" onClick={() => downloadJson("regression-case.json", data.regression)}>Download regression case</button>
            </p>
            <JsonBlock label="Full result" value={data.details} />
          </>
        )}
      />
    </Section>
  );
}

interface ResolvedBody {
  pins: { world_version: string; environment_id: string; environment_version: string; adapter_ref: string; code_digest: string; manifest_digest: string | null; target_id: string; target_version: string; discovered_tools: string[]; discovered_modes: string[]; discovered_fixtures: string[] };
  effective: { target: unknown; fixture: { name: string }; scenario: Record<string, unknown>; faults: Array<{ hook: string; required: boolean }>; rules: string[]; limits: Record<string, number>; repetitions: { count: number } };
  provenance: { policy_version: string; capability_source: string; adapter_origin: string; qualification_status: string };
}

interface RunBody {
  status: string; termination_reason: string; tool_calls: number; wall_clock_ms: number;
  failed_rules: string[]; checks: Record<string, boolean>; faults_configured: string[];
  faults_fired: string[] | null; faults_status: string; denial: string | null;
  limits_exceeded: string[]; evidence_digest: string | null;
  evidence: { trace: unknown[]; state: Record<string, unknown> } | null;
}

function ExperimentPanel({ source, detail, fixtureName, snapshot }: { source: DataSource; detail: ExtEnvironmentDetail; fixtureName: string; snapshot: Record<string, unknown> | null }) {
  const [policy, setPolicy] = useState<Result<ExpPolicy> | null>(null);
  const [name, setName] = useState("console-experiment");
  const [targetKind, setTargetKind] = useState<"registered" | "script">("script");
  const [targetId, setTargetId] = useState(detail.targets[0] ?? "");
  const [script, setScript] = useState<string>(() => defaultScript(detail.world_version, snapshot));
  const [faults, setFaults] = useState<Record<string, { on: boolean; required: boolean }>>(() =>
    Object.fromEntries(detail.faults.map((hook) => [hook, { on: false, required: true }])));
  const [rules, setRules] = useState<string[]>(() => [...detail.invariants]);
  const [maxCalls, setMaxCalls] = useState("10");
  const [maxTotal, setMaxTotal] = useState("20");
  const [maxWall, setMaxWall] = useState("5000");
  const [count, setCount] = useState("2");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [saved, setSaved] = useState<Result<ExpRequested> | null>(null);
  const [resolved, setResolved] = useState<Result<ExpResolved> | null>(null);
  const [repetition, setRepetition] = useState("1");
  const [attempt, setAttempt] = useState("1");
  const [run, setRun] = useState<Result<ExpRun> | null>(null);
  const [leftId, setLeftId] = useState("");
  const [rightId, setRightId] = useState("");
  const [diff, setDiff] = useState<Result<ExpDiff> | null>(null);

  useEffect(() => {
    let cancelled = false;
    source.getExpPolicy().then((result) => {
      if (!cancelled) setPolicy(result);
    });
    return () => {
      cancelled = true;
    };
  }, [source]);

  function toggleFault(hook: string, key: "on" | "required") {
    setFaults((prev) => { const current = prev[hook] ?? { on: false, required: true }; const next = key === "on" ? { on: !current.on, required: current.required } : { on: current.on, required: !current.required }; return { ...prev, [hook]: next }; });
  }

  function toggleRule(rule: string) {
    setRules((prev) => (prev.includes(rule) ? prev.filter((name) => name !== rule) : [...prev, rule]));
  }

  function buildConfig(): { config?: Record<string, unknown>; error?: string } {
    const limits = { max_tool_calls: Number(maxCalls), max_total_tool_calls: Number(maxTotal), max_wall_clock_ms: Number(maxWall) };
    if (![limits.max_tool_calls, limits.max_total_tool_calls, limits.max_wall_clock_ms, Number(count)].every((n) => Number.isInteger(n) && n >= 1)) {
      return { error: "Limits and repetitions must be positive integers" };
    }
    let target: Record<string, unknown>;
    if (targetKind === "registered") {
      if (targetId === "") return { error: "Pick a registered target" };
      target = { kind: "registered", id: targetId };
    } else {
      const parsed = parseJson(script);
      if (parsed.error !== undefined) return { error: parsed.error };
      target = { kind: "script", actions: parsed.value };
    }
    return {
      config: {
        config_version: 1, name, world_version: detail.world_version, target,
        fixture: { name: fixtureName },
        faults: Object.entries(faults).filter(([, v]) => v.on).map(([hook, v]) => ({ hook, params: {}, required: v.required })),
        rules: [...rules], limits, repetitions: { count: Number(count) },
      },
    };
  }

  async function save() {
    const built = buildConfig();
    if (built.error !== undefined || built.config === undefined) {
      setFailure({ kind: "transport", message: built.error ?? "Invalid config" });
      return;
    }
    setFailure(null);
    setBusy(true);
    try {
      const result = await source.createExpConfig({ config: built.config as never });
      setSaved(result);
      setResolved(null);
      setRun(null);
    } finally {
      setBusy(false);
    }
  }

  async function resolve() {
    if (saved === null || !saved.ok) return;
    setFailure(null);
    setBusy(true);
    try {
      const result = await source.resolveExpConfig(saved.data.config_id, { manifest_digest: null });
      setResolved(result);
      setRun(null);
      if (result.ok) {
        setLeftId((prev) => prev || result.data.resolved_id);
        setRightId(result.data.resolved_id);
      }
    } finally {
      setBusy(false);
    }
  }

  async function runOnce() {
    if (resolved === null || !resolved.ok) return;
    const rep = Number(repetition);
    const att = Number(attempt);
    if (!Number.isInteger(rep) || rep < 1 || !Number.isInteger(att) || att < 1) {
      setFailure({ kind: "transport", message: "Repetition and attempt must be positive integers" });
      return;
    }
    setFailure(null);
    setBusy(true);
    try {
      setRun(await source.runExpResolved(resolved.data.resolved_id, { repetition: rep, attempt: att }));
    } finally {
      setBusy(false);
    }
  }

  async function compare() {
    if (leftId === "" || rightId === "") {
      setFailure({ kind: "transport", message: "Enter two resolved config ids" });
      return;
    }
    setFailure(null);
    setBusy(true);
    try {
      setDiff(await source.diffExpResolved(leftId, rightId));
    } finally {
      setBusy(false);
    }
  }

  const ceilings = policy !== null && policy.ok ? policy.data : null;
  const overCeiling =
    ceilings !== null &&
    (Number(maxCalls) > ceilings.ceiling_tool_calls ||
      Number(maxTotal) > ceilings.ceiling_total_tool_calls ||
      Number(maxWall) > ceilings.ceiling_wall_clock_ms ||
      Number(count) > ceilings.ceiling_repetitions);

  return (
    <Section title="Experiment configs">
      <p className="muted">
        Saved configs resolve to pinned versions before anything runs. The server validates every field;{` `}
        {ceilings === null ? "loading policy ceilings." : `ceilings: ${ceilings.ceiling_tool_calls} calls/run, ${ceilings.ceiling_total_tool_calls} total, ${ceilings.ceiling_repetitions} repetitions, ${ceilings.ceiling_wall_clock_ms} ms.`}
      </p>
      <TextInput label="Name" value={name} onChange={setName} />
      <label className="field">
        <span>Target</span>
        <select value={targetKind} onChange={(event) => setTargetKind(event.target.value as "registered" | "script")}>
          <option value="script">Inline script</option>
          <option value="registered">Registered target</option>
        </select>
      </label>
      {targetKind === "registered" ? (
        <label className="field">
          <span>Registered target id</span>
          <select value={targetId} onChange={(event) => setTargetId(event.target.value)}>
            {detail.targets.map((id) => <option key={id} value={id}>{id}</option>)}
          </select>
        </label>
      ) : (
        <JsonArea label="Script actions" value={script} onChange={setScript} />
      )}
      <p>
        <span className="muted">Faults: </span>
        {detail.faults.map((hook) => (
          <label key={hook} style={{ marginRight: "1em" }}>
            <input type="checkbox" checked={faults[hook]?.on ?? false} onChange={() => toggleFault(hook, "on")} /> {hook}
            {faults[hook]?.on === true ? (
              <label style={{ marginLeft: "0.5em" }}>
                <input type="checkbox" checked={faults[hook]?.required ?? false} onChange={() => toggleFault(hook, "required")} /> required
              </label>
            ) : null}
          </label>
        ))}
      </p>
      <p>
        <span className="muted">Rules: </span>
        {detail.invariants.map((rule) => (
          <label key={rule} style={{ marginRight: "1em" }}>
            <input type="checkbox" checked={rules.includes(rule)} onChange={() => toggleRule(rule)} /> {rule}
          </label>
        ))}
      </p>
      <TextInput label="Max tool calls per run" value={maxCalls} onChange={setMaxCalls} />
      <TextInput label="Max total tool calls" value={maxTotal} onChange={setMaxTotal} />
      <TextInput label="Max wall clock (ms)" value={maxWall} onChange={setMaxWall} />
      <TextInput label="Repetitions" value={count} onChange={setCount} />
      {overCeiling ? <p><Tag tone="warn">Over policy ceiling; the server will refuse this config</Tag></p> : null}
      <p>
        <button type="button" disabled={busy || overCeiling} onClick={() => void save()}>{busy ? "Working." : "Save config"}</button>{" "}
        <button type="button" disabled={busy || saved === null || !saved.ok} onClick={() => void resolve()}>{busy ? "Working." : "Resolve"}</button>
      </p>
      {failure !== null ? <FailureNotice failure={failure} /> : null}
      <Outcome
        result={saved}
        render={(data) => (
          <p><Mono>{data.config_id}</Mono> <span className="muted">digest {data.digest.slice(0, 16)} by {data.requested_by}</span></p>
        )}
      />
      <Outcome
        result={resolved}
        render={(data) => {
          const body = data.resolved as unknown as ResolvedBody;
          return (
            <>
              <Facts
                facts={[
                  { term: "Resolved", detail: <Mono>{data.resolved_id}</Mono> },
                  { term: "Qualification", detail: <Tag tone={body.provenance.qualification_status.startsWith("qualified") ? "ok" : "warn"}>{body.provenance.qualification_status}</Tag> },
                  { term: "Target", detail: <><Mono>{body.pins.target_id}</Mono> <Mono>{body.pins.target_version}</Mono></> },
                  { term: "Code", detail: <Mono>{body.pins.code_digest.slice(0, 16)}</Mono> },
                  { term: "Fixture", detail: <Mono>{body.effective.fixture.name}</Mono> },
                  { term: "Scenario", detail: <Mono>{JSON.stringify(body.effective.scenario)}</Mono> },
                ]}
              />
              <TextInput label="Repetition" value={repetition} onChange={setRepetition} />
              <TextInput label="Attempt" value={attempt} onChange={setAttempt} />
              <p><button type="button" disabled={busy} onClick={() => void runOnce()}>{busy ? "Running." : "Run repetition"}</button></p>
            </>
          );
        }}
      />
      <Outcome
        result={run}
        render={(data) => {
          const body = data.run as unknown as RunBody;
          const requiredUntested =
            resolved !== null &&
            resolved.ok &&
            body.faults_fired === null &&
            (resolved.data.resolved as unknown as ResolvedBody).effective.faults.some((fault) => fault.required);
          const tone = body.status !== "completed" ? "warn" : body.failed_rules.length > 0 ? "fail" : requiredUntested ? "warn" : "ok";
          return (
            <>
              <p>
                <Tag tone={tone}>{body.status}</Tag>{" "}
                <span className="muted">{body.termination_reason}; {body.tool_calls} calls in {body.wall_clock_ms} ms</span>
              </p>
              <Facts
                facts={[
                  { term: "Failed rules", detail: <span>{body.failed_rules.length > 0 ? body.failed_rules.map((name) => <Mono key={name}>{name} </Mono>) : "none"}</span> },
                  { term: "Checks", detail: <span>{Object.entries(body.checks).map(([check, hit]) => <span key={check}><Tag tone={hit ? "ok" : "fail"}>{check}</Tag> </span>)}</span> },
                  { term: "Faults fired", detail: <span className="muted">{body.faults_fired === null ? `unknown (${body.faults_status})` : body.faults_fired.join(", ") || "none"}</span> },
                  { term: "Evidence", detail: <Mono>{body.evidence_digest ?? "none"}</Mono> },
                ]}
              />
              {body.denial !== null ? <p><Tag tone="warn">denied</Tag> <span className="muted">{body.denial}</span></p> : null}
              {requiredUntested ? <p><Tag tone="warn">untested</Tag> <span className="muted">a required fault fired nowhere the world reports; this is not a clean pass</span></p> : null}
              {body.limits_exceeded.length > 0 ? <p className="muted">Limits exceeded: {body.limits_exceeded.join(", ")}</p> : null}
              {body.evidence !== null ? <JsonBlock label={`Trace (${body.evidence.trace.length} events)`} value={body.evidence.trace} /> : null}
            </>
          );
        }}
      />
      <TextInput label="Compare left resolved id" value={leftId} onChange={setLeftId} />
      <TextInput label="Compare right resolved id" value={rightId} onChange={setRightId} />
      <p><button type="button" disabled={busy} onClick={() => void compare()}>{busy ? "Working." : "Compare resolved"}</button></p>
      <Outcome
        result={diff}
        render={(data) => (
          <>
            <p><Tag tone={data.equal ? "ok" : "neutral"}>{data.equal ? "equal" : `${data.diff_count} differences`}</Tag></p>
            {data.diffs.map((entry) => (
              <p key={entry.path}><Mono>{entry.path}</Mono> <span className="muted">{JSON.stringify(entry.left)} {"->"} {JSON.stringify(entry.right)}</span></p>
            ))}
          </>
        )}
      />
    </Section>
  );
}