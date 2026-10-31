import type {
  CampaignView,
  ExperimentView,
  RunView,
  WorldOutcomeError,
  WorldOutcomeResult,
  WorldTraceEvent,
} from "../api/api.generated.ts";
import type { DataSource, Origin } from "../data/source.ts";
import { coverageOf } from "../logic/coverage.ts";
import {
  alignTraces,
  isErrorOutcome,
  metricRows,
  refundLedger,
  violatedInvariants,
  violatingEventIds,
  type RefundLedger,
} from "../logic/evidence.ts";
import { formatMinor, prettyJson } from "../logic/format.ts";
import { campaignHref } from "../logic/routes.ts";
import { differences } from "../logic/stateDiff.ts";
import { FailureNotice, JsonBlock, Mono, Notice, Section, Tag } from "../ui/components.tsx";
import { EvidenceLinks } from "./EvidencePage.tsx";
import {
  CoverageLine,
  CoverageNotice,
  ExperimentGate,
  ExperimentPicker,
  useExperiment,
  useRun,
} from "./experimentData.tsx";

export function DiffPage({
  source,
  view,
  experimentId,
}: {
  source: DataSource;
  view: CampaignView;
  experimentId: string | null;
}) {
  const { resource } = useExperiment(source, view, experimentId);
  const { resource: baseline } = useRun(source, view, experimentId, "baseline", resource.status === "ready");
  const { resource: fork } = useRun(source, view, experimentId, "fork", resource.status === "ready");
  return (
    <ExperimentGate experimentId={experimentId} resource={resource}>
      {(experiment, origins) => (
        <>
          <ExperimentPicker view={view} current={experiment.experiment_id} target="diff" />
          {baseline.status === "failed" ? <FailureNotice failure={baseline.failure} /> : null}
          {fork.status === "failed" ? <FailureNotice failure={fork.failure} /> : null}
          <DiffPanel
            campaignId={view.record.campaign_id}
            experiment={experiment}
            origins={origins}
            baseline={baseline.status === "ready" ? baseline.data : null}
            fork={fork.status === "ready" ? fork.data : null}
            runOrigins={[
              ...(baseline.status === "ready" ? baseline.origins : []),
              ...(fork.status === "ready" ? fork.origins : []),
            ]}
          />
        </>
      )}
    </ExperimentGate>
  );
}

export function DiffPanel({
  campaignId,
  experiment,
  origins,
  baseline,
  fork,
  runOrigins,
}: {
  campaignId: string;
  experiment: ExperimentView;
  origins: readonly Origin[];
  baseline: RunView | null;
  fork: RunView | null;
  runOrigins: readonly Origin[];
}) {
  const { diff, findings, experiment_id: experimentId } = experiment;
  const first = diff.first_divergence;
  const violating = violatingEventIds(findings);
  const violated = violatedInvariants(diff, findings);
  const link = (eventId: number) => campaignHref(campaignId, { view: "evidence", experimentId, run: "fork", eventId });
  const rows = alignTraces(baseline?.result ?? null, fork?.result ?? null);
  const runsMissing = baseline === null || fork === null;
  const baselineLedger = refundLedger(baseline?.result ?? null);
  const forkLedger = refundLedger(fork?.result ?? null);
  const coverage = coverageOf(experiment, baseline, fork);

  return (
    <>
      {coverage === null ? null : <CoverageNotice coverage={coverage.coverage} violations={violating.length} />}
      <Section title="Where the worlds separate" origins={origins}>
        {coverage === null ? null : <CoverageLine coverage={coverage.coverage} recorded={coverage.recorded} />}
        <div className="callouts">
          <div className="callout" data-testid="first-divergence">
            <h3>First divergence</h3>
            {first === null ? (
              <p>No divergence recorded.</p>
            ) : (
              <>
                <p className="callout-value">
                  <a href={link(first.event_id)}>Event {first.event_id}</a>
                </p>
                <p className="muted">The first event where the baseline and fork records differ.</p>
              </>
            )}
          </div>
          <div className="callout" data-testid="violating-events" data-tone="fail">
            <h3>Violation evidence</h3>
            {violating.length === 0 ? (
              <p>The verifier cites no failing event.</p>
            ) : (
              <>
                <p className="callout-value">
                  {violating.map((id, index) => (
                    <span key={id}>
                      {index > 0 ? ", " : null}
                      <a href={link(id)}>Event {id}</a>
                    </span>
                  ))}
                </p>
                <p className="muted">
                  {first !== null && !violating.includes(first.event_id)
                    ? `A different event from the first divergence. Event ${first.event_id} is where the worlds separated; the verifier cites this one for the violation.`
                    : "The events the verifier cites for its failed findings."}
                </p>
              </>
            )}
          </div>
        </div>
      </Section>

      <Section title="Violated invariants" origins={origins}>
        {violated.length === 0 ? (
          <Notice state="empty" title="The diff lists no violated invariants" />
        ) : (
          <ul className="findings">
            {violated.map(({ invariant, findings: cited }) => (
              <li key={invariant} data-tone="fail">
                <div>
                  <Tag tone="fail">violated</Tag> <Mono>{invariant}</Mono>
                </div>
                {cited.length === 0 ? (
                  <p className="muted">No verifier finding was supplied for this invariant.</p>
                ) : (
                  cited.map((finding) => (
                    <div key={finding.finding_id}>
                      {finding.observed_value === null ? null : (
                        <p>
                          Observed <Mono>{finding.observed_value}</Mono> against limit{" "}
                          <Mono>{finding.limit_value ?? "none"}</Mono>
                          {finding.unit === null ? null : <span className="muted"> {finding.unit}</span>}
                        </p>
                      )}
                      <p>
                        <EvidenceLinks
                          ids={finding.evidence_event_ids}
                          campaignId={campaignId}
                          experimentId={experimentId}
                          run="fork"
                        />
                      </p>
                      <p className="muted">
                        <Mono>{finding.finding_id}</Mono>, {finding.verifier_version}
                      </p>
                    </div>
                  ))
                )}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="World metrics" origins={origins}>
        <MetricTable experiment={experiment} currency={forkLedger?.currency ?? null} />
      </Section>

      {baselineLedger === null && forkLedger === null ? null : (
        <Section title="Ledger" origins={runOrigins}>
          <p className="muted">A synthetic ledger. No real money moved.</p>
          <div className="sides">
            <LedgerColumn side="baseline" title="Baseline" ledger={baselineLedger} />
            <LedgerColumn side="fork" title="Fork" ledger={forkLedger} />
          </div>
        </Section>
      )}

      {first === null ? null : (
        <Section title={`Baseline and fork at event ${first.event_id}`} origins={origins}>
          <div className="sides">
            <DivergenceColumn side="baseline" title="Baseline" event={first.baseline} />
            <DivergenceColumn side="fork" title="Fork" event={first.faulted} />
          </div>
          <DifferenceTable baseline={first.baseline} fork={first.faulted} />
        </Section>
      )}

      <Section title="Every event, side by side" origins={runOrigins}>
        {runsMissing ? (
          <Notice state="loading" title="Waiting for both recorded runs" />
        ) : (
          <div className="table-wrap" tabIndex={0} role="region" aria-label="Baseline and fork events">
            <table className="aligned">
              <thead>
                <tr>
                  <th scope="col">Event</th>
                  <th scope="col" data-side="baseline">
                    Baseline
                  </th>
                  <th scope="col" data-side="fork">
                    Fork
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.eventId}>
                    <th scope="row">
                      <a href={link(row.eventId)}>{row.eventId}</a>
                      {row.eventId === first?.event_id ? (
                        <>
                          {" "}
                          <Tag tone="active">First divergence</Tag>
                        </>
                      ) : null}
                      {violating.includes(row.eventId) ? (
                        <>
                          {" "}
                          <Tag tone="fail">Violation evidence</Tag>
                        </>
                      ) : null}
                    </th>
                    <td data-label="Baseline">
                      <EventCell event={row.baseline} />
                    </td>
                    <td data-label="Fork">
                      <EventCell event={row.fork} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </>
  );
}

function MetricTable({ experiment, currency }: { experiment: ExperimentView; currency: string | null }) {
  const rows = metricRows(experiment.diff);
  if (rows.length === 0) return <Notice state="empty" title="The diff records no metrics" />;
  const shown = (name: string, value: number | null) => {
    if (value === null) return <span className="muted">not recorded</span>;
    return name.endsWith("_minor") && currency !== null ? (
      <>
        <Mono>{value}</Mono> <span className="muted">{formatMinor(value, currency)}</span>
      </>
    ) : (
      <Mono>{value}</Mono>
    );
  };
  return (
    <div className="table-wrap" tabIndex={0} role="region" aria-label="World metrics">
      <table>
        <thead>
          <tr>
            <th scope="col">Metric</th>
            <th scope="col" data-side="baseline">
              Baseline
            </th>
            <th scope="col" data-side="fork">
              Fork
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.name}>
              <th scope="row">
                <Mono>{row.name}</Mono>
              </th>
              <td data-label="Baseline">{shown(row.name, row.baseline)}</td>
              <td data-label="Fork">{shown(row.name, row.fork)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LedgerColumn({ side, title, ledger }: { side: string; title: string; ledger: RefundLedger | null }) {
  return (
    <article className="side" data-side={side}>
      <h3>{title}</h3>
      {ledger === null ? (
        <p className="muted">Waiting for this recorded run</p>
      ) : (
        <>
          <p>
            Total refunded <Mono>{ledger.totalRefundedMinor ?? "not recorded"}</Mono> minor units
            {ledger.totalRefundedMinor === null || ledger.currency === null ? null : (
              <span className="muted"> {formatMinor(ledger.totalRefundedMinor, ledger.currency)}</span>
            )}
          </p>
          {ledger.paidMinor === null ? null : (
            <p className="muted">
              Paid <Mono>{ledger.paidMinor}</Mono> minor units
            </p>
          )}
          <ul className="plain">
            {ledger.rows.map((row) => (
              <li key={row.transaction_id}>
                <Mono>{row.transaction_id}</Mono> <Mono>{row.amount_minor}</Mono>{" "}
                <span className="muted">key {row.idempotency_key}</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </article>
  );
}

function outcomeLabel(outcome: WorldOutcomeResult | WorldOutcomeError): string {
  return isErrorOutcome(outcome) ? `${outcome.error}: ${outcome.message}` : "ok";
}

function DivergenceColumn({ side, title, event }: { side: string; title: string; event: WorldTraceEvent | null }) {
  return (
    <article className="side" data-side={side}>
      <h3>{title}</h3>
      {event === null ? (
        <p className="muted">This run has no event here.</p>
      ) : (
        <>
          <p>
            <Mono>{event.request.tool}</Mono>
          </p>
          <p>{outcomeLabel(event.outcome)}</p>
          <JsonBlock label={`${title} request and outcome`} value={{ request: event.request, outcome: event.outcome }} />
          <JsonBlock label={`${title} world state`} value={event.state} />
        </>
      )}
    </article>
  );
}

function shownValue(value: unknown) {
  return value === undefined ? <span className="muted">absent</span> : <Mono>{prettyJson(value)}</Mono>;
}

export function DifferenceTable({ baseline, fork }: { baseline: WorldTraceEvent | null; fork: WorldTraceEvent | null }) {
  const rows = differences(baseline, fork);
  return (
    <>
      <h3>What differs at this event</h3>
      {rows.length === 0 ? (
        <p className="muted">The two records are identical.</p>
      ) : (
        <div className="table-wrap" tabIndex={0} role="region" aria-label="Recorded differences at the first divergence">
          <table data-testid="difference-table">
            <thead>
              <tr>
                <th scope="col">Recorded field</th>
                <th scope="col" data-side="baseline">
                  Baseline
                </th>
                <th scope="col" data-side="fork">
                  Fork
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.path}>
                  <th scope="row">
                    <Mono>{row.path}</Mono>
                  </th>
                  <td data-label="Baseline">{shownValue(row.baseline)}</td>
                  <td data-label="Fork">{shownValue(row.fork)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted">A comparison of the two records. It does not say which side is right.</p>
    </>
  );
}

function EventCell({ event }: { event: WorldTraceEvent | null }) {
  if (event === null) return <span className="muted">no event</span>;
  const failed = isErrorOutcome(event.outcome);
  return (
    <>
      <Mono>{event.request.tool}</Mono>
      {failed ? (
        <>
          {" "}
          <Tag tone="warn">{(event.outcome as WorldOutcomeError).error}</Tag>
        </>
      ) : null}
    </>
  );
}

