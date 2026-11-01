import { useEffect, useRef } from "react";
import type {
  CampaignView,
  ExperimentEvent,
  ExperimentView,
  RunView,
  WorldInvariantFinding,
  WorldTraceEvent,
} from "../api/api.generated.ts";
import type { DataSource, Origin, RunName } from "../data/source.ts";
import { readCoverage } from "../logic/coverage.ts";
import { findingsForEvent, isErrorOutcome, parseEventRef, violatingEventIds } from "../logic/evidence.ts";
import { formatTimestamp } from "../logic/format.ts";
import { deliveryAt, deliveryLabel, sharingLostAt } from "../logic/journeyStory.ts";
import { campaignHref, type Route } from "../logic/routes.ts";
import { Facts, FailureNotice, JsonBlock, Loading, Mono, Notice, Section, Tag } from "../ui/components.tsx";
import { CoverageNotice, ExperimentGate, ExperimentPicker, LostFaultsNotice, useExperiment, useRun } from "./experimentData.tsx";

type CampaignRoute = Extract<Route, { page: "campaign" }>;

const RUN_LABEL: Record<RunName, string> = { baseline: "Baseline", fork: "Fork", repaired: "Repaired" };

export function EvidencePage({
  source,
  view,
  experimentId,
  route,
  events,
  eventOrigins,
}: {
  source: DataSource;
  view: CampaignView;
  experimentId: string | null;
  route: CampaignRoute;
  events: readonly ExperimentEvent[];
  eventOrigins: readonly Origin[];
}) {
  const { resource } = useExperiment(source, view, experimentId);
  const ready = resource.status === "ready" ? resource.data : null;
  const runName = route.run ?? "fork";
  const available = (ready?.runs as readonly RunName[] | undefined)?.includes(runName) ?? false;
  const { resource: run } = useRun(source, view, experimentId, runName, available);

  return (
    <>
      <ExperimentGate experimentId={experimentId} resource={resource}>
        {(experiment, origins) => (
          <>
            <ExperimentPicker view={view} current={experiment.experiment_id} target="evidence" />
            <ProposalSection experiment={experiment} origins={origins} />
            <RunPicker view={view} experiment={experiment} current={runName} />
            {runName === "repaired" ? <LostFaultsNotice repair={view.repair} /> : null}
            {!available ? (
              <Notice state="empty" title={`No ${RUN_LABEL[runName].toLowerCase()} run recorded`} />
            ) : run.status === "loading" ? (
              <Loading what="run" />
            ) : run.status === "failed" ? (
              <FailureNotice failure={run.failure} />
            ) : (
              <RunEvidence
                campaignId={view.record.campaign_id}
                experiment={experiment}
                run={run.data}
                origins={run.origins}
                focusEventId={route.eventId}
              />
            )}
          </>
        )}
      </ExperimentGate>
      <ApplicationEvents
        events={events}
        origins={eventOrigins}
        campaignId={view.record.campaign_id}
        experimentId={experimentId}
      />
    </>
  );
}

export function ProposalSection({ experiment, origins }: { experiment: ExperimentView; origins: readonly Origin[] }) {
  const { proposal, execution } = experiment;
  const coverage = readCoverage(experiment);
  return (
    <Section title="Experiment" origins={origins}>
      {coverage === null ? null : (
        <CoverageNotice coverage={coverage} violations={violatingEventIds(experiment.findings).length} />
      )}
      <Facts
        facts={[
          { term: "Hypothesis (proposal text, untrusted)", detail: proposal.hypothesis },
          { term: "Expected signal (proposal text, untrusted)", detail: proposal.expected_signal },
          { term: "Adapter", detail: <Mono>{proposal.adapter_id}</Mono> },
          {
            term: "Execution mode",
            detail:
              execution === null ? (
                <span className="muted">not recorded for this world</span>
              ) : (
                <>
                  <Mono>{execution.mode}</Mono>, guarded <Mono>{String(execution.guarded)}</Mono>
                </>
              ),
          },
        ]}
      />
    </Section>
  );
}

function RunPicker({ view, experiment, current }: { view: CampaignView; experiment: ExperimentView; current: RunName }) {
  const names = experiment.runs as readonly RunName[];
  return (
    <nav className="sub-nav" aria-label="Runs">
      <ul>
        {names.map((name) => (
          <li key={name}>
            <a
              data-side={name}
              href={campaignHref(view.record.campaign_id, {
                view: "evidence",
                experimentId: experiment.experiment_id,
                run: name,
              })}
              {...(name === current ? { "aria-current": "page" as const } : {})}
            >
              {RUN_LABEL[name]}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}

export function RunEvidence({
  campaignId,
  experiment,
  run,
  origins,
  focusEventId,
}: {
  campaignId: string;
  experiment: ExperimentView;
  run: RunView;
  origins: readonly Origin[];
  focusEventId: number | null;
}) {
  const { result, findings } = run;
  const firstDivergence = run.run === "repaired" ? null : (experiment.diff.first_divergence?.event_id ?? null);
  const violating = violatingEventIds(findings);
  return (
    <>
      <Section title={`${RUN_LABEL[run.run]} run`} origins={origins}>
        {result.status === "completed" ? null : (
          <Notice state="incomplete" title="This run stopped before finishing">
            Its status is <Mono>{result.status}</Mono>. Passing checks on it prove nothing. A failure recorded before it
            stopped still stands.
          </Notice>
        )}
        <Facts
          facts={[
            { term: "Run", detail: <Mono>{run.run_id}</Mono> },
            { term: "Status", detail: <Mono>{result.status}</Mono> },
            { term: "Adapter", detail: <Mono>{result.adapter_id}</Mono> },
            {
              term: "Execution",
              detail:
                run.execution === null ? (
                  <span className="muted">not recorded for this world</span>
                ) : (
                  <>
                    <Mono>{run.execution.mode}</Mono>, guarded <Mono>{String(run.execution.guarded)}</Mono>
                  </>
                ),
            },
          ]}
        />
        <h3>Verifier checks</h3>
        <ul className="checks">
          {Object.entries(result.checks).map(([name, passed]) => (
            <li key={name}>
              <Tag tone={passed ? "neutral" : "fail"}>{passed ? "passed" : "failed"}</Tag> <Mono>{name}</Mono>
            </li>
          ))}
        </ul>
      </Section>
      <Section title="Findings" origins={origins}>
        <FindingList findings={findings} campaignId={campaignId} experimentId={experiment.experiment_id} run={run.run} />
      </Section>
      <Section title="Tool events" origins={origins}>
        {result.trace.length === 0 ? (
          <Notice state="empty" title="This run recorded no tool events" />
        ) : (
          <ol className="events">
            {result.trace.map((event) => (
              <ToolEvent
                key={event.event_id}
                event={event}
                findings={findingsForEvent(findings, event.event_id)}
                isFirstDivergence={event.event_id === firstDivergence}
                isViolating={violating.includes(event.event_id)}
                focused={event.event_id === focusEventId}
              />
            ))}
          </ol>
        )}
      </Section>
    </>
  );
}

export function FindingList({
  findings,
  campaignId,
  experimentId,
  run,
}: {
  findings: readonly WorldInvariantFinding[];
  campaignId: string;
  experimentId: string;
  run: RunName;
}) {
  if (findings.length === 0) return <Notice state="empty" title="No findings for this run" />;
  return (
    <ul className="findings">
      {findings.map((finding) => (
        <li key={finding.finding_id} data-tone={finding.verdict === "failed" ? "fail" : "neutral"}>
          <div>
            <Tag tone={finding.verdict === "failed" ? "fail" : "neutral"}>{finding.verdict}</Tag>{" "}
            <Mono>{finding.invariant}</Mono>
          </div>
          {finding.observed_value === null ? null : (
            <p>
              Observed <Mono>{finding.observed_value}</Mono> against limit <Mono>{finding.limit_value ?? "none"}</Mono>
              {finding.unit === null ? null : <span className="muted"> {finding.unit}</span>}
            </p>
          )}
          <p>
            <EvidenceLinks ids={finding.evidence_event_ids} campaignId={campaignId} experimentId={experimentId} run={run} />
          </p>
          <p className="muted">
            <Mono>{finding.finding_id}</Mono>, {finding.verifier_version}
          </p>
        </li>
      ))}
    </ul>
  );
}

export function EvidenceLinks({
  ids,
  campaignId,
  experimentId,
  run,
}: {
  ids: readonly number[];
  campaignId: string;
  experimentId: string;
  run: RunName;
}) {
  if (ids.length === 0) return <span className="muted">No evidence events cited</span>;
  return (
    <span>
      Evidence events{" "}
      {ids.map((id, index) => (
        <span key={id}>
          {index > 0 ? ", " : null}
          <a href={campaignHref(campaignId, { view: "evidence", experimentId, run, eventId: id })}>{id}</a>
        </span>
      ))}
    </span>
  );
}

function ToolEvent({
  event,
  findings,
  isFirstDivergence,
  isViolating,
  focused,
}: {
  event: WorldTraceEvent;
  findings: readonly WorldInvariantFinding[];
  isFirstDivergence: boolean;
  isViolating: boolean;
  focused: boolean;
}) {
  const item = useRef<HTMLLIElement | null>(null);
  useEffect(() => {
    if (!focused) return;
    item.current?.focus();
    item.current?.scrollIntoView({ block: "center" });
  }, [focused]);
  const failed = isErrorOutcome(event.outcome);
  const delivery = deliveryAt(event);
  const lostSharing = sharingLostAt(event);
  return (
    <li ref={item} {...(focused ? { tabIndex: -1 } : {})} data-tone={failed ? "warn" : "neutral"} data-testid={`event-${event.event_id}`}>
      <div className="event-head">
        <span className="event-id">{event.event_id}</span>
        <Mono>{event.request.tool}</Mono>
        {failed ? <Tag tone="warn">{(event.outcome as { error: string }).error}</Tag> : null}
        {isFirstDivergence ? <Tag tone="active">First divergence</Tag> : null}
        {isViolating ? <Tag tone="fail">Violation evidence</Tag> : null}
        {lostSharing.map((participant) => (
          <Tag key={participant} tone="warn">
            {participant} lost sharing
          </Tag>
        ))}
      </div>
      {delivery === null ? null : (
        <p data-testid={`delivery-${event.event_id}`}>
          {delivery.denied === null ? "Delivered" : "Denied"} {deliveryLabel(delivery)}. Their sharing at this event:{" "}
          <Mono>{delivery.sharing === null ? "not recorded" : String(delivery.sharing)}</Mono>
        </p>
      )}
      {failed ? <p>{(event.outcome as { message: string }).message}</p> : null}
      {findings.length > 0 ? (
        <p className="muted">
          Cited by{" "}
          {findings.map((finding, index) => (
            <span key={finding.finding_id}>
              {index > 0 ? ", " : null}
              <Mono>{finding.invariant}</Mono>
            </span>
          ))}
        </p>
      ) : null}
      <JsonBlock label={`Event ${event.event_id} request and outcome`} value={{ request: event.request, outcome: event.outcome }} />
      <JsonBlock label={`Event ${event.event_id} world state`} value={event.state} />
    </li>
  );
}

export function ApplicationEvents({
  events,
  origins,
  campaignId,
  experimentId,
}: {
  events: readonly ExperimentEvent[];
  origins: readonly Origin[];
  campaignId: string;
  experimentId: string | null;
}) {
  return (
    <Section title="Campaign events" origins={origins}>
      {events.length === 0 ? (
        <Notice state="empty" title="No campaign events yet" />
      ) : (
        <ol className="app-events">
          {events.map((event) => (
            <li key={event.sequence}>
              <span className="event-id">{event.sequence}</span>
              <div>
                <p>
                  <Mono>{event.kind}</Mono> <span className="muted">{formatTimestamp(event.occurred_at)}</span>
                </p>
                <p>{event.summary}</p>
                {event.refs.length === 0 ? null : (
                  <ul className="refs">
                    {event.refs.map((ref) => {
                      const parsed = ref.kind === "event" ? parseEventRef(ref.id) : null;
                      return (
                        <li key={`${ref.kind}/${ref.id}`}>
                          {parsed === null ? (
                            <span>
                              {ref.kind} <Mono>{ref.id}</Mono>
                            </span>
                          ) : (
                            <a href={campaignHref(campaignId, { view: "evidence", experimentId, eventId: parsed.eventId })}>
                              Event {parsed.eventId} of <Mono>{parsed.runId}</Mono>
                            </a>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                )}
              </div>
            </li>
          ))}
        </ol>
      )}
    </Section>
  );
}

