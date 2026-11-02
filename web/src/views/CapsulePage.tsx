import { useRef, useState } from "react";
import type { CampaignView, CapsuleInfo, ExecutionRecord, ExperimentView, ReplayResult } from "../api/api.generated.ts";
import type { DataSource, Failure, Origin, RunName } from "../data/source.ts";
import { readCoverage } from "../logic/coverage.ts";
import { violatingEventIds } from "../logic/evidence.ts";
import { presentReplay } from "../logic/replay.ts";
import type { Tone } from "../logic/repair.ts";
import { BusyNote, Digest, Facts, FailureNotice, Mono, Section, Tag } from "../ui/components.tsx";
import { CoverageNotice, ExperimentGate, ExperimentPicker, LostFaultsNotice, useExperiment } from "./experimentData.tsx";

const RUN_LABEL: Record<RunName, string> = { baseline: "Baseline", fork: "Fork", repaired: "Repaired" };

export function CapsulePage({
  source,
  view,
  experimentId,
}: {
  source: DataSource;
  view: CampaignView;
  experimentId: string | null;
}) {
  const { resource } = useExperiment(source, view, experimentId);
  return (
    <ExperimentGate experimentId={experimentId} resource={resource}>
      {(experiment, origins) => (
        <>
          <ExperimentPicker view={view} current={experiment.experiment_id} target="capsule" />
          <CapsuleCoverage experiment={experiment} />
          <CapsuleControls source={source} view={view} experiment={experiment} origins={origins} />
        </>
      )}
    </ExperimentGate>
  );
}

function CapsuleCoverage({ experiment }: { experiment: ExperimentView }) {
  const coverage = readCoverage(experiment);
  if (coverage === null) return null;
  return <CoverageNotice coverage={coverage} violations={violatingEventIds(experiment.findings).length} />;
}

function CapsuleControls({
  source,
  view,
  experiment,
  origins,
}: {
  source: DataSource;
  view: CampaignView;
  experiment: ExperimentView;
  origins: readonly Origin[];
}) {
  const campaignId = view.record.campaign_id;
  const runs = experiment.runs as readonly RunName[];
  const [run, setRun] = useState<RunName>(runs.includes("fork") ? "fork" : (runs[0] ?? "fork"));
  const [capsule, setCapsule] = useState<{ run: RunName; info: CapsuleInfo; origins: readonly Origin[] } | null>(null);
  const [replay, setReplay] = useState<{ run: RunName; result: ReplayResult; origins: readonly Origin[] } | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [busyAction, setBusyAction] = useState<"export" | "replay" | "download" | null>(null);
  const busy = busyAction !== null;
  const download = useRef<HTMLAnchorElement | null>(null);
  const id = experiment.experiment_id;
  const exported = capsule?.run === run ? capsule : null;
  const replayed = replay?.run === run ? replay : null;

  async function act(action: "export" | "replay" | "download", work: () => Promise<void>) {
    setBusyAction(action);
    setFailure(null);
    await work();
    setBusyAction(null);
  }

  const exportRun = () =>
    act("export", async () => {
      const result = await source.exportCapsule(campaignId, id, run);
      if (result.ok) setCapsule({ run, info: result.data, origins: result.origins });
      else setFailure(result.failure);
    });

  const replayRun = () =>
    act("replay", async () => {
      const result = await source.replayCapsule(campaignId, id, run);
      if (result.ok) setReplay({ run, result: result.data, origins: result.origins });
      else setFailure(result.failure);
    });

  const save = (info: CapsuleInfo) =>
    act("download", async () => {
      const result = await source.downloadArtifact(info.artifact.id);
      if (!result.ok) {
        setFailure(result.failure);
        return;
      }
      const url = URL.createObjectURL(result.data);
      const anchor = download.current;
      if (anchor !== null) {
        anchor.href = url;
        anchor.download = `${info.artifact.id}.json`;
        anchor.click();
      }
      // Revoked after the click has been handled: some browsers cancel a download otherwise.
      setTimeout(() => URL.revokeObjectURL(url), 0);
    });

  return (
    <>
      <Section title="Failure Capsule" origins={origins}>
        <p>
          A capsule holds the recorded actions of one run. Replay re-executes them in the world and makes no model call.
        </p>
        <div className="stack-form">
          <label htmlFor="capsule-run">Run</label>
          <select
            id="capsule-run"
            value={run}
            disabled={busy}
            onChange={(event) => {
              setRun(event.currentTarget.value as RunName);
              setFailure(null);
            }}
          >
            {runs.map((name) => (
              <option key={name} value={name}>
                {RUN_LABEL[name]}
              </option>
            ))}
          </select>
          <div className="actions">
            <button type="button" disabled={busy} onClick={() => void exportRun()}>
              Export capsule
            </button>
            <button type="button" disabled={busy || exported === null} onClick={() => void replayRun()}>
              Replay capsule
            </button>
            <button
              type="button"
              className="quiet"
              disabled={busy || exported === null}
              onClick={() => {
                if (exported !== null) void save(exported.info);
              }}
            >
              Download
            </button>
            <a ref={download} hidden aria-hidden={true}>
              Download link
            </a>
          </div>
          {busyAction === "export" ? (
            <BusyNote>Exporting the capsule…</BusyNote>
          ) : busyAction === "replay" ? (
            <BusyNote>Replaying the capsule…</BusyNote>
          ) : busyAction === "download" ? (
            <BusyNote>Preparing the download…</BusyNote>
          ) : null}
          {exported === null ? <p className="muted">Export a capsule to replay or download it.</p> : null}
        </div>
        {failure === null ? null : <FailureNotice failure={failure} />}
      </Section>
      {exported === null ? null : (
        <Section title="Exported capsule" origins={exported.origins}>
          <Facts
            facts={[
              { term: "Artifact", detail: <Mono>{exported.info.artifact.id}</Mono> },
              { term: "World", detail: <Mono>{exported.info.world_version}</Mono> },
              { term: "Capsule schema", detail: <Mono>{exported.info.capsule_schema_version}</Mono> },
              {
                term: "Checksum",
                detail: exported.info.artifact.sha256 === null ? <span className="muted">not recorded</span> : <Digest value={exported.info.artifact.sha256} />,
              },
            ]}
          />
          <p className="muted">A checksum detects accidental change. It does not prove who made the capsule.</p>
        </Section>
      )}
      {run === "repaired" ? <LostFaultsNotice repair={view.repair} /> : null}
      {replayed === null ? null : (
        <ReplayPanel
          result={replayed.result}
          origins={replayed.origins}
          lostFaults={run === "repaired" ? (view.repair?.lost_faults ?? []) : []}
          faultNeverFired={run === "fork" && experiment.coverage?.faults_triggered === false}
        />
      )}
      {replayed === null ? null : (
        <EnforcementPanel execution={experiment.execution} checks={replayed.result.checks} origins={replayed.origins} />
      )}
    </>
  );
}

export function ReplayPanel({
  result,
  origins,
  lostFaults = [],
  faultNeverFired = false,
}: {
  result: ReplayResult;
  origins: readonly Origin[];
  lostFaults?: readonly string[];
  faultNeverFired?: boolean;
}) {
  const shown = presentReplay(result, lostFaults, faultNeverFired);
  return (
    <Section title="Replay result" origins={origins}>
      <dl className="recorded" data-testid="replay-fields">
        <div>
          <dt>
            <Mono>reproduced</Mono>
          </dt>
          <dd>
            <Mono>{String(result.reproduced)}</Mono>
          </dd>
        </div>
        <div>
          <dt>
            <Mono>run_status</Mono>
          </dt>
          <dd>
            {result.run_status === null ? (
              <span className="muted">not reported: the capsule did not reproduce</span>
            ) : (
              <Mono>{result.run_status}</Mono>
            )}
          </dd>
        </div>
        <div>
          <dt>
            <Mono>model_calls</Mono>
          </dt>
          <dd>
            <Mono>{result.model_calls}</Mono>
          </dd>
        </div>
      </dl>
      <div className="verdict" data-tone={shown.tone} data-testid="replay-verdict">
        <strong>{shown.headline}</strong>
        <p>{shown.detail}</p>
      </div>
      <h3>What was compared</h3>
      <ul className="checks">
        {Object.entries(result.matches).map(([name, matched]) => (
          <li key={name}>
            <Tag tone={matched ? "neutral" : "fail"}>{matched ? "matched" : "differs"}</Tag> <Mono>{name}</Mono>
          </li>
        ))}
      </ul>
      <h3>Checks after replay</h3>
      <ul className="checks">
        {Object.entries(result.checks).map(([name, passed]) => (
          <li key={name}>
            <Tag tone={passed ? "neutral" : "fail"}>{passed ? "passed" : "failed"}</Tag> <Mono>{name}</Mono>
          </li>
        ))}
      </ul>
      <h3>Metrics after replay</h3>
      <ul className="checks">
        {Object.entries(result.metrics).map(([name, value]) => (
          <li key={name}>
            <Mono>{name}</Mono> <Mono>{value}</Mono>
          </li>
        ))}
      </ul>
      {result.guarded === null ? null : (
        <p className="muted">
          Recorded with <Mono>guarded</Mono> <Mono>{String(result.guarded)}</Mono>.
        </p>
      )}
    </Section>
  );
}

export interface EnforcementPresentation {
  tone: Tone;
  headline: string;
  detail: string;
}

/**
 * The enforcement outcome is the third reproduction outcome, beside replay
 * and retest. It exists only for enforced runs: a lab run's checks are
 * findings, never an enforcement verdict.
 */
export function presentEnforcement(
  execution: ExecutionRecord | null,
  checks: Record<string, boolean | undefined> | null,
): EnforcementPresentation {
  if (execution === null) {
    return {
      tone: "neutral",
      headline: "No recorded execution mode",
      detail: "This run recorded no execution mode, so there is no enforcement outcome to assess.",
    };
  }
  if (execution.mode !== "enforced" || execution.guarded !== true) {
    return {
      tone: "neutral",
      headline: "Lab run: enforcement was off",
      detail: "The guard was off for this run, so its checks are findings, not an enforcement outcome.",
    };
  }
  if (checks === null) {
    return {
      tone: "neutral",
      headline: "Enforcement needs the recorded checks",
      detail: "Replay the capsule to read the checks this enforced run recorded.",
    };
  }
  const failed = Object.entries(checks)
    .filter(([, passed]) => !passed)
    .map(([name]) => name);
  if (failed.length === 0) {
    return {
      tone: "ok",
      headline: "Enforcement held",
      detail: "Every check passed with the guard on: nothing got through the enforced boundary.",
    };
  }
  return {
    tone: "fail",
    headline: "Enforcement violated",
    detail: `The guard was on and these checks still failed: ${failed.join(", ")}.`,
  };
}

export function EnforcementPanel({
  execution,
  checks,
  origins,
}: {
  execution: ExecutionRecord | null;
  checks: Record<string, boolean | undefined> | null;
  origins: readonly Origin[];
}) {
  const shown = presentEnforcement(execution, checks);
  const enforced = execution !== null && execution.mode === "enforced" && execution.guarded === true;
  const failed = checks === null ? null : Object.entries(checks).filter(([, passed]) => !passed);
  const held = enforced && failed !== null && failed.length === 0;
  return (
    <Section title="Enforcement outcome" origins={origins}>
      <dl className="recorded" data-testid="enforcement-fields">
        <div>
          <dt>
            <Mono>mode</Mono>
          </dt>
          <dd>{execution === null ? <span className="muted">not recorded</span> : <Mono>{execution.mode}</Mono>}</dd>
        </div>
        <div>
          <dt>
            <Mono>guarded</Mono>
          </dt>
          <dd>{execution === null ? <span className="muted">not recorded</span> : <Mono>{String(execution.guarded)}</Mono>}</dd>
        </div>
        <div>
          <dt>
            <Mono>held</Mono>
          </dt>
          <dd>
            {enforced && failed !== null ? (
              <Mono>{String(held)}</Mono>
            ) : (
              <span className="muted">no enforcement outcome for this run</span>
            )}
          </dd>
        </div>
      </dl>
      <div className="verdict" data-tone={shown.tone} data-testid="enforcement-verdict">
        <strong>{shown.headline}</strong>
        <p>{shown.detail}</p>
      </div>
      {checks === null ? null : (
        <>
          <h3>Checks under enforcement</h3>
          <ul className="checks">
            {Object.entries(checks).map(([name, passed]) => (
              <li key={name}>
                <Tag tone={passed === true ? "neutral" : "fail"}>{passed === true ? "passed" : "failed"}</Tag> <Mono>{name}</Mono>
              </li>
            ))}
          </ul>
        </>
      )}
    </Section>
  );
}

