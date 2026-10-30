import { useRef, useState } from "react";
import type { CampaignView, CapsuleInfo, ExperimentView, ReplayResult } from "../api/api.generated.ts";
import type { DataSource, Failure, Origin, RunName } from "../data/source.ts";
import { Digest, Facts, FailureNotice, Mono, Section, Tag } from "../ui/components.tsx";
import { ExperimentGate, ExperimentPicker, useExperiment } from "./experimentData.tsx";

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
          <CapsuleControls source={source} campaignId={view.record.campaign_id} experiment={experiment} origins={origins} />
        </>
      )}
    </ExperimentGate>
  );
}

function CapsuleControls({
  source,
  campaignId,
  experiment,
  origins,
}: {
  source: DataSource;
  campaignId: string;
  experiment: ExperimentView;
  origins: readonly Origin[];
}) {
  const runs = experiment.runs as readonly RunName[];
  const [run, setRun] = useState<RunName>(runs.includes("fork") ? "fork" : (runs[0] ?? "fork"));
  const [capsule, setCapsule] = useState<{ run: RunName; info: CapsuleInfo; origins: readonly Origin[] } | null>(null);
  const [replay, setReplay] = useState<{ run: RunName; result: ReplayResult; origins: readonly Origin[] } | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [busy, setBusy] = useState(false);
  const download = useRef<HTMLAnchorElement | null>(null);
  const id = experiment.experiment_id;
  const exported = capsule?.run === run ? capsule : null;
  const replayed = replay?.run === run ? replay : null;

  async function act(work: () => Promise<void>) {
    setBusy(true);
    setFailure(null);
    await work();
    setBusy(false);
  }

  const exportRun = () =>
    act(async () => {
      const result = await source.exportCapsule(campaignId, id, run);
      if (result.ok) setCapsule({ run, info: result.data, origins: result.origins });
      else setFailure(result.failure);
    });

  const replayRun = () =>
    act(async () => {
      const result = await source.replayCapsule(campaignId, id, run);
      if (result.ok) setReplay({ run, result: result.data, origins: result.origins });
      else setFailure(result.failure);
    });

  const save = (info: CapsuleInfo) =>
    act(async () => {
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
                {name}
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
      {replayed === null ? null : <ReplayPanel result={replayed.result} origins={replayed.origins} />}
    </>
  );
}

export function ReplayPanel({ result, origins }: { result: ReplayResult; origins: readonly Origin[] }) {
  const failing = Object.entries(result.checks).filter(([, passed]) => !passed);
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
            <Mono>model_calls</Mono>
          </dt>
          <dd>
            <Mono>{result.model_calls}</Mono>
          </dd>
        </div>
      </dl>
      {!result.reproduced ? (
        <div className="verdict" data-tone="fail" data-testid="replay-verdict">
          <strong>Not reproduced</strong>
          <p>The recorded evidence did not repeat, so this capsule's claims don't hold.</p>
        </div>
      ) : failing.length > 0 ? (
        <div className="verdict" data-tone="warn" data-testid="replay-verdict">
          <strong>Failure reproduced</strong>
          <p>The recorded failure repeated. Reproducing it is not a pass: these checks still fail.</p>
        </div>
      ) : (
        <div className="verdict" data-tone="neutral" data-testid="replay-verdict">
          <strong>Reproduced</strong>
          <p>The recorded run repeated and every check it records passed.</p>
        </div>
      )}
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

