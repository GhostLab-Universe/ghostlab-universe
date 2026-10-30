import { useState } from "react";
import type { BudgetLimits, BudgetUsage, CampaignView, RepairView } from "../api/api.generated.ts";
import type { DataSource, Failure, Origin, Result } from "../data/source.ts";
import { formatCount, formatTimestamp } from "../logic/format.ts";
import { presentRepair } from "../logic/repair.ts";
import { campaignHref } from "../logic/routes.ts";
import { taskStatus } from "../logic/status.ts";
import { orderTasks } from "../logic/timeline.ts";
import { Facts, FailureNotice, Mono, Notice, RecordedErrors, RefLabel, Section, Tag } from "../ui/components.tsx";

export function CampaignPanel({
  view,
  origins,
  source,
  refresh,
}: {
  view: CampaignView;
  origins: readonly Origin[];
  source: DataSource;
  refresh: () => Promise<void>;
}) {
  const { record } = view;
  return (
    <>
      {record.status === "awaiting_approval" ? <AwaitingApproval view={view} /> : null}
      {record.status === "cancelled" ? (
        <Notice state="cancelled" title="Campaign cancelled">
          It stopped at a guarded step. Evidence recorded before the stop is still shown.
        </Notice>
      ) : null}
      {record.status === "failed" ? <Notice state="failed" title="Campaign failed" /> : null}
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
          ]}
        />
      </Section>
      <Section title="Budget" origins={origins}>
        <BudgetTable limits={record.spec.budget} usage={record.usage} />
      </Section>
      <Section title="Tasks" origins={origins}>
        <TaskTimeline view={view} />
      </Section>
    </>
  );
}

function AwaitingApproval({ view }: { view: CampaignView }) {
  return (
    <Notice state="awaiting_approval" title="Awaiting approval" live>
      <p>The campaign is paused until a person decides on the repair.</p>
      {view.approval === null ? null : (
        <p>
          <a href={campaignHref(view.record.campaign_id, { view: "approval" })}>Review the approval</a>
        </p>
      )}
    </Notice>
  );
}

function JobControls({ view, source, refresh }: { view: CampaignView; source: DataSource; refresh: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const { status } = view.record;
  const canCancel = (status === "running" || status === "queued") && !view.job.cancel_requested;
  const stalled = view.job.state === "stalled";

  async function act(run: () => Promise<Result<CampaignView>>) {
    setBusy(true);
    setFailure(null);
    const result = await run();
    if (!result.ok) setFailure(result.failure);
    await refresh();
    setBusy(false);
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
      <div className="actions">
        {canCancel ? (
          <button type="button" disabled={busy} onClick={() => void act(() => source.cancelCampaign(id))}>
            Cancel campaign
          </button>
        ) : null}
        {stalled ? (
          <button type="button" disabled={busy} onClick={() => void act(() => source.resumeCampaign(id))}>
            Resume campaign
          </button>
        ) : null}
      </div>
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

function BudgetTable({ limits, usage }: { limits: BudgetLimits; usage: BudgetUsage }) {
  const rows: Array<[string, string, string]> = [
    ["Model calls", formatCount(usage.model_calls), formatCount(limits.max_model_calls)],
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
                <td>{used}</td>
                <td>{limit}</td>
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

function TaskTimeline({ view }: { view: CampaignView }) {
  const ordered = orderTasks(view.tasks);
  if (ordered.length === 0) return <Notice state="empty" title="No tasks yet" />;
  const position = new Map(ordered.map((item, index) => [item.task.task_id, index + 1]));
  return (
    <ol className="timeline">
      {ordered.map(({ task, unknownDependencies }) => {
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
  );
}
