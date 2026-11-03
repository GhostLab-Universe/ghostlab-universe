import { useState } from "react";
import type { BudgetLimits, BudgetUsage, CampaignView, RepairView } from "../api/api.generated.ts";
import type { DataSource, Failure, Origin, Result } from "../data/source.ts";
import { looksExpired } from "../logic/approval.ts";
import { readCoverage } from "../logic/coverage.ts";
import { formatCount, formatTimestamp } from "../logic/format.ts";
import { presentRepair } from "../logic/repair.ts";
import { summarizeProgress, type ProgressLive } from "../logic/progress.ts";
import { campaignHref } from "../logic/routes.ts";
import { campaignStatus, outcomeNotice, proposalCallsLabel, taskStatus } from "../logic/status.ts";
import { orderTasks } from "../logic/timeline.ts";
import { BusyNote, Digest, Facts, FailureNotice, FilterBar, Mono, Notice, RecordedErrors, RefLabel, Section, Tag } from "../ui/components.tsx";
import { CoverageLine } from "./experimentData.tsx";

export function CampaignPanel({
  view,
  origins,
  source,
  refresh,
  live,
}: {
  view: CampaignView;
  origins: readonly Origin[];
  source: DataSource;
  refresh: () => Promise<void>;
  /** Live refresh state. Without it the panel assumes every reported event is shown. */
  live?: ProgressLive;
}) {
  const { record } = view;
  const notice = outcomeNotice(view.outcome);
  const progressLive: ProgressLive = live ?? { connection: "live", cursor: view.event_count };
  return (
    <>
      {record.status === "awaiting_approval" ? <AwaitingApproval view={view} /> : null}
      {record.status === "cancelled" ? (
        <Notice state="cancelled" title="Campaign cancelled">
          It stopped at a guarded step. Evidence recorded before the stop is still shown.
        </Notice>
      ) : null}
      {notice !== null ? (
        <Notice state={notice.state} title={notice.title} live>
          {notice.detail}
        </Notice>
      ) : record.status === "failed" ? (
        <Notice state="failed" title="Campaign failed" />
      ) : null}
      <ProgressPanel view={view} origins={origins} live={progressLive} onRefresh={refresh} />
      {view.errors.length > 0 ? (
        <Section title="Recorded errors" origins={origins}>
          <RecordedErrors errors={view.errors} />
        </Section>
      ) : null}
      {view.repair === null ? null : (
        <RepairResult repair={view.repair} outcome={view.outcome} worldVersion={record.spec.world_version} origins={origins} />
      )}
      <JobControls view={view} source={source} refresh={refresh} />
      <Section title="Objective" origins={origins}>
        <p>{record.spec.objective}</p>
        <h3>Success criteria</h3>
        <ul className="plain">
          {record.spec.success_criteria.map((criterion, index) => (
            <li key={index}>{criterion}</li>
          ))}
        </ul>
        <Facts
          facts={[
            { term: "Created", detail: formatTimestamp(record.created_at) },
            { term: "Last status change", detail: formatTimestamp(record.updated_at) },
            { term: "Created by", detail: <Mono>{record.created_by.principal_id}</Mono> },
            { term: "Trace", detail: <Mono>{record.trace_id}</Mono> },
            {
              term: "Pinned fixture",
              detail:
                view.fixture_sha256 === null ? (
                  <span className="muted">not recorded on this older checkpoint</span>
                ) : (
                  <Digest value={view.fixture_sha256} />
                ),
            },
          ]}
        />
      </Section>
      <Section title="Experiments" origins={origins}>
        <ExperimentList view={view} />
      </Section>
      <Section title="Budget" origins={origins}>
        <BudgetTable limits={record.spec.budget} usage={record.usage} inference={view.inference} />
      </Section>
      <Section title="Tasks" origins={origins}>
        <TaskTimeline view={view} />
      </Section>
    </>
  );
}

/** Plain words for each progress segment. The bar stays decorative: the sentence below it is the accessible record. */
const SEGMENT_LABEL: Record<string, string> = {
  done: "succeeded",
  active: "in progress",
  waiting: "waiting",
  failed: "failed",
  stopped: "stopped",
};

/** Live progress as recorded: status, task counts, events shown and refresh state. */
export function ProgressPanel({
  view,
  origins,
  live,
  onRefresh,
}: {
  view: CampaignView;
  origins: readonly Origin[];
  live: ProgressLive;
  /** Manual re-read. The poller refreshes on its own; this is for "now". */
  onRefresh?: () => Promise<void>;
}) {
  const progress = summarizeProgress(view, live);
  const status = campaignStatus(view.record.status, view.outcome);
  const [refreshing, setRefreshing] = useState(false);
  const { tasks } = progress;
  const parts: string[] = [];
  if (tasks.succeeded > 0) parts.push(`${tasks.succeeded} succeeded`);
  if (tasks.active > 0) parts.push(`${tasks.active} in progress`);
  if (tasks.waiting > 0) parts.push(`${tasks.waiting} waiting`);
  if (tasks.failed > 0) parts.push(`${tasks.failed} failed`);
  if (tasks.stopped > 0) parts.push(`${tasks.stopped} stopped`);
  const segments: Array<{ seg: string; count: number }> = [
    { seg: "done", count: tasks.succeeded },
    { seg: "active", count: tasks.active },
    { seg: "waiting", count: tasks.waiting },
    { seg: "failed", count: tasks.failed },
    { seg: "stopped", count: tasks.stopped },
  ];
  return (
    <Section title="Progress" origins={origins}>
      <div data-testid="campaign-progress">
        <div className="progress-head">
          {/* Named apart from the campaign heading's status line: one .status-line per page. */}
          <p className="progress-facts">
            <Tag tone={status.tone}>{status.label}</Tag>
            <span>
              {progress.experiments === 0 ? "No experiments yet" : `${progress.experiments} experiment${progress.experiments === 1 ? "" : "s"}`}
            </span>
            <span>
              Events {progress.eventsSeen} of {progress.eventsTotal} shown
            </span>
            <span className="muted">Worker {view.job.state}</span>
          </p>
          {onRefresh === undefined ? null : (
            <button
              type="button"
              className="quiet"
              disabled={refreshing}
              onClick={() => {
                setRefreshing(true);
                void onRefresh().finally(() => setRefreshing(false));
              }}
            >
              {refreshing ? "Refreshing" : "Refresh now"}
            </button>
          )}
        </div>
        {tasks.total === 0 ? (
          <p className="muted">No tasks recorded yet.</p>
        ) : (
          <>
            <div className="progress-bar" aria-hidden="true">
              {segments
                .filter((item) => item.count > 0)
                .map((item) => (
                  <span
                    key={item.seg}
                    data-seg={item.seg}
                    title={`${item.count} ${SEGMENT_LABEL[item.seg] ?? item.seg}`}
                    style={{ width: `${(item.count / tasks.total) * 100}%` }}
                  />
                ))}
            </div>
            <p>
              {tasks.total} task{tasks.total === 1 ? "" : "s"}: {parts.join(", ")}
            </p>
          </>
        )}
        <p className="muted">{progress.note}</p>
      </div>
    </Section>
  );
}

function ExperimentList({ view }: { view: CampaignView }) {
  if (view.experiments.length === 0) {
    return (
      <Notice state="empty" title="No experiments yet">
        Experiments appear here once the campaign runs.
      </Notice>
    );
  }
  return (
    <ul className="findings">
      {view.experiments.map((experiment) => {
        const coverage = readCoverage(experiment);
        return (
          <li key={experiment.experiment_id} data-tone={coverage?.complete === false ? "warn" : "neutral"}>
            <p>
              Iteration {experiment.iteration}, adapter <Mono>{experiment.adapter_id}</Mono>
            </p>
            <p className="muted">Hypothesis (proposal text, untrusted): {experiment.hypothesis}</p>
            {coverage === null ? (
              <p className="muted">Coverage was not recorded for this experiment.</p>
            ) : (
              <CoverageLine coverage={coverage} recorded />
            )}
            {coverage?.complete === false ? <Tag tone="warn">Not a complete test</Tag> : null}
          </li>
        );
      })}
    </ul>
  );
}

function AwaitingApproval({ view }: { view: CampaignView }) {
  const expired = looksExpired(view.approval?.expires_at, Date.now());
  return (
    <Notice state="awaiting_approval" title={expired ? "Approval expired" : "Awaiting approval"} live>
      <p>
        {expired
          ? "This approval is past its expiry, so it can no longer be decided."
          : "The campaign is paused until a person decides on the repair."}
      </p>
      {view.approval === null ? null : (
        <p>
          <a href={campaignHref(view.record.campaign_id, { view: "approval" })}>Review the approval</a>
        </p>
      )}
    </Notice>
  );
}

function JobControls({ view, source, refresh }: { view: CampaignView; source: DataSource; refresh: () => Promise<void> }) {
  const [jobAction, setJobAction] = useState<"cancel" | "resume" | null>(null);
  const busy = jobAction !== null;
  const [failure, setFailure] = useState<Failure | null>(null);
  const { status } = view.record;
  const paused = status === "awaiting_approval";
  const canCancel = (status === "running" || status === "queued" || paused) && !view.job.cancel_requested;
  const stalled = view.job.state === "stalled";

  async function act(name: "cancel" | "resume", run: () => Promise<Result<CampaignView>>) {
    setJobAction(name);
    setFailure(null);
    const result = await run();
    if (!result.ok) setFailure(result.failure);
    await refresh();
    setJobAction(null);
  }

  if (!canCancel && !stalled && !view.job.cancel_requested && failure === null) return null;
  const id = view.record.campaign_id;
  return (
    <Section title="Execution">
      {stalled ? (
        <Notice state="failed" title="Stalled">
          <p>The campaign is unfinished and no worker holds it.</p>
          {view.job.error === null ? null : <p>{view.job.error.message}</p>}
        </Notice>
      ) : null}
      {view.job.cancel_requested ? <p>Cancel requested. The campaign stops at its next guarded step.</p> : null}
      {paused && canCancel ? (
        <p className="muted">Cancelling ends this campaign. Its approval stays unspent and can&apos;t run anything.</p>
      ) : null}
      <div className="actions">
        {canCancel ? (
          <button type="button" disabled={busy} onClick={() => void act("cancel", () => source.cancelCampaign(id))}>
            Cancel campaign
          </button>
        ) : null}
        {stalled ? (
          <button type="button" disabled={busy} onClick={() => void act("resume", () => source.resumeCampaign(id))}>
            Resume campaign
          </button>
        ) : null}
      </div>
      {jobAction === "cancel" ? (
        <BusyNote>Cancelling the campaign…</BusyNote>
      ) : jobAction === "resume" ? (
        <BusyNote>Resuming the campaign…</BusyNote>
      ) : null}
      {failure === null ? null : <FailureNotice failure={failure} />}
    </Section>
  );
}

export function RepairResult({
  repair,
  outcome,
  worldVersion,
  origins,
}: {
  repair: RepairView;
  outcome: string | null;
  worldVersion: string;
  origins: readonly Origin[];
}) {
  const shown = presentRepair(repair, outcome, worldVersion);
  return (
    <Section title="Repair result" origins={origins}>
      <div className="verdict" data-tone={shown.tone} data-testid="repair-verdict">
        <strong>{shown.headline}</strong>
        <p>{shown.detail}</p>
      </div>
      <p className="muted">As recorded by the campaign.</p>
      <dl className="recorded" data-testid="repair-fields">
        {shown.fields.map((field) => (
          <div key={field.label}>
            <dt>
              <Mono>{field.label}</Mono>
            </dt>
            <dd>{field.recorded ? <Mono>{field.value}</Mono> : <span className="muted">{field.value}</span>}</dd>
          </div>
        ))}
      </dl>
      {repair.task_outcome === null ? null : (
        <>
          <h3>Task outcome</h3>
          <dl className="recorded">
            {Object.entries(repair.task_outcome).map(([name, value]) => (
              <div key={name}>
                <dt>
                  <Mono>{name}</Mono>
                </dt>
                <dd>
                  <Mono>{String(value)}</Mono>
                </dd>
              </div>
            ))}
          </dl>
        </>
      )}
      {repair.guard_denials === null || repair.guard_denials.length === 0 ? null : (
        <>
          <h3>Guard denials</h3>
          <ul className="plain">
            {repair.guard_denials.map((denial) => (
              <li key={denial.event_id}>
                Event {denial.event_id}: <Mono>{denial.error}</Mono>
              </li>
            ))}
          </ul>
        </>
      )}
      <Facts
        facts={[
          ...(repair.adapter === null ? [] : [{ term: "Adapter", detail: <Mono>{repair.adapter}</Mono> }]),
          ...(repair.execution === null
            ? []
            : [
                {
                  term: "Execution",
                  detail: (
                    <>
                      <Mono>{repair.execution.mode}</Mono>, guarded <Mono>{String(repair.execution.guarded)}</Mono>
                    </>
                  ),
                },
              ]),
          ...(repair.refunded_minor === null
            ? []
            : [{ term: "Refunded (minor units)", detail: <Mono>{repair.refunded_minor}</Mono> }]),
          ...(repair.run === null ? [] : [{ term: "Repaired run", detail: <RefLabel value={repair.run} /> }]),
        ]}
      />
    </Section>
  );
}

function BudgetTable({
  limits,
  usage,
  inference,
}: {
  limits: BudgetLimits;
  usage: BudgetUsage;
  inference: CampaignView["inference"];
}) {
  const rows: Array<[string, string, string]> = [
    [proposalCallsLabel(inference), formatCount(usage.model_calls), formatCount(limits.max_model_calls)],
    ["Tool calls", formatCount(usage.tool_calls), formatCount(limits.max_tool_calls)],
    ["Input tokens", formatCount(usage.input_tokens), formatCount(limits.max_input_tokens)],
    ["Output tokens", formatCount(usage.output_tokens), formatCount(limits.max_output_tokens)],
    ["Wall clock (ms)", formatCount(usage.wall_clock_ms), formatCount(limits.max_wall_clock_ms)],
    [
      "Cost",
      usage.cost_minor === null ? "not reported" : `${formatCount(usage.cost_minor)} ${usage.cost_currency ?? ""} minor units`,
      limits.max_cost_minor === null ? "no limit set" : `${formatCount(limits.max_cost_minor)} ${limits.cost_currency ?? ""} minor units`,
    ],
  ];
  return (
    <>
      <div className="table-wrap" tabIndex={0} role="region" aria-label="Budget usage">
        <table>
          <thead>
            <tr>
              <th scope="col">Resource</th>
              <th scope="col">Used</th>
              <th scope="col">Limit</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([name, used, limit]) => (
              <tr key={name}>
                <th scope="row">{name}</th>
                <td data-label="Used">{used}</td>
                <td data-label="Limit">{limit}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {usage.unknown_usage_events > 0 ? (
        <p className="muted">
          {usage.unknown_usage_events} usage events reported no counts, so token figures may be low.
        </p>
      ) : null}
    </>
  );
}

type TaskFilter = "all" | "attention" | "finished";

const ATTENTION = new Set(["failed", "running", "awaiting_approval"]);
const FINISHED = new Set(["succeeded", "cancelled", "skipped"]);

function TaskTimeline({ view }: { view: CampaignView }) {
  const ordered = orderTasks(view.tasks);
  const [filter, setFilter] = useState<TaskFilter>("all");
  if (ordered.length === 0) {
    return (
      <Notice state="empty" title="No tasks yet">
        The service has not recorded any tasks for this campaign yet.
      </Notice>
    );
  }
  const shown =
    filter === "all" ? ordered : ordered.filter(({ task }) => (filter === "attention" ? ATTENTION : FINISHED).has(task.status));
  const position = new Map(ordered.map((item, index) => [item.task.task_id, index + 1]));
  return (
    <>
      <FilterBar
        label="Show"
        value={filter}
        onChange={setFilter}
        options={[
          { value: "all", label: "All tasks", count: ordered.length },
          { value: "attention", label: "Needs attention", count: ordered.filter(({ task }) => ATTENTION.has(task.status)).length },
          { value: "finished", label: "Finished", count: ordered.filter(({ task }) => FINISHED.has(task.status)).length },
        ]}
      />
      {shown.length === 0 ? (
        <Notice state="empty" title="No tasks match this filter">
          The campaign records no {filter === "attention" ? "running, awaiting decision or failed" : "finished"} tasks.
        </Notice>
      ) : (
    <ol className="timeline">
      {shown.map(({ task, unknownDependencies }) => {
        const status = taskStatus(task.status);
        const after = task.depends_on.flatMap((id) => position.get(id) ?? []);
        return (
          <li key={task.task_id} data-tone={status.tone}>
            <div className="timeline-head">
              <strong>{task.kind.replaceAll("_", " ")}</strong> <Tag tone={status.tone}>{status.label}</Tag>
            </div>
            <p className="muted">
              {task.role}, attempt {task.attempt} of {task.max_attempts}
              {after.length > 0 ? `, after step ${after.join(" and ")}` : ""}
            </p>
            <p>{task.exit_condition}</p>
            <p className="muted">
              <Mono>{task.task_id}</Mono>
              {task.output_ref === null ? null : (
                <>
                  {" "}
                  output <RefLabel value={task.output_ref} />
                </>
              )}
            </p>
            {unknownDependencies.length > 0 ? (
              <p>
                <Tag tone="warn">Unknown dependency</Tag> <Mono>{unknownDependencies.join(", ")}</Mono>
              </p>
            ) : null}
          </li>
        );
      })}
      </ol>
      )}
    </>
  );
}
