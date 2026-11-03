import { useRef, useState } from "react";
import type { ApprovalView, CampaignView, DecisionResult, SessionInfo } from "../api/api.generated.ts";
import type { DataSource, Failure, Origin } from "../data/source.ts";
import { actionFacts, buildSubmission, decisionGate, IdempotencyKeys, stoppedNote, type Decision } from "../logic/approval.ts";
import { readCoverage } from "../logic/coverage.ts";
import { BLOCKED_ENDED_HELP, CAMPAIGN_FINISHED, describeFailure, failureState } from "../logic/failure.ts";
import { formatTimestamp, prettyJson } from "../logic/format.ts";
import { isTerminal } from "../logic/status.ts";
import { BusyNote, Digest, Facts, FailureNotice, Loading, Mono, Notice, Section, Tag } from "../ui/components.tsx";
import { useResource } from "../ui/hooks.ts";
import { RepairResult } from "./CampaignPage.tsx";
import { CoverageNotice } from "./experimentData.tsx";

export function ApprovalPage({
  source,
  view,
  session,
  refresh,
}: {
  source: DataSource;
  view: CampaignView;
  session: SessionInfo | null;
  refresh: () => Promise<void>;
}) {
  const approvalId = view.approval?.approval_id ?? null;
  const { resource, reload } = useResource(approvalId, view.event_count, () => source.getApproval(approvalId ?? ""));
  const keys = useRef(new IdempotencyKeys());
  const [busyAction, setBusyAction] = useState<"decide" | "resume" | null>(null);
  const busy = busyAction !== null;
  const [resumeLost, setResumeLost] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [result, setResult] = useState<DecisionResult | null>(null);
  const decisionRegion = useRef<HTMLDivElement | null>(null);

  if (approvalId === null) {
    return (
      <Notice state="empty" title="No approval requested">
        An approval appears here when a repair needs a decision.
      </Notice>
    );
  }
  if (resource.status === "loading") return <Loading what="approval" />;
  if (resource.status === "failed") return <FailureNotice failure={resource.failure} />;
  const truncated = view.experiments.flatMap((experiment) => readCoverage(experiment) ?? []).find((item) => !item.complete);
  // Approved but never consumed, so a reload still shows why nothing has run.
  const blocked = view.outcome === "settings_changed";
  const stopped = isTerminal(view.record.status);
  const unspent =
    resource.data.effective_status === "approved" &&
    view.repair === null &&
    view.job.state !== "active" &&
    !blocked &&
    !stopped;

  async function decide(approval: ApprovalView, decision: Decision, reason: string) {
    setBusyAction("decide");
    setFailure(null);
    setResumeLost(false);
    setResult(null);
    const submission = buildSubmission(approval, decision, reason);
    const outcome = await source.decideApproval(submission, keys.current.keyFor(submission));
    if (outcome.ok) setResult(outcome.data);
    else setFailure(outcome.failure);
    // What the page shows next comes from the service, not from the response above.
    await refresh();
    reload();
    setBusyAction(null);
    // The buttons are gone by now, so keep keyboard users where the result appears.
    decisionRegion.current?.focus();
  }

  async function resume() {
    setBusyAction("resume");
    setFailure(null);
    setResumeLost(false);
    const outcome = await source.resumeCampaign(view.record.campaign_id);
    if (outcome.ok) setResult(null);
    else {
      setFailure(outcome.failure);
      setResumeLost(outcome.failure.kind === "transport");
    }
    await refresh();
    reload();
    setBusyAction(null);
  }

  return (
    <>
      {/* An approval is only requested after a failed finding, so a violation exists. */}
      {truncated === undefined ? null : <CoverageNotice coverage={truncated} violations={1} />}
      <ApprovalDetail approval={resource.data} origins={resource.origins} />
      <div ref={decisionRegion} tabIndex={-1} data-testid="decision-region">
        <DecisionPanel
          approval={resource.data}
          session={session}
          busy={busy}
          busyAction={busyAction}
          failure={failure}
          result={currentResult(result, resumeLost, stopped, resource.data.effective_status)}
          origins={resource.origins}
          unspent={unspent}
          blocked={blocked}
          stoppedText={stoppedNote(resource.data.effective_status, stopped, view.repair !== null)}
          onDecide={(choice, reason) => void decide(resource.data, choice, reason)}
          resumeLost={resumeLost && !stopped}
          onResume={() => void resume()}
        />
      </div>
      {view.repair === null ? null : (
        <RepairResult
          repair={view.repair}
          outcome={view.outcome}
          worldVersion={view.record.spec.world_version}
          origins={resource.origins}
        />
      )}
    </>
  );
}

/** A refused decide stops being the news once a resume whose response was lost landed. */
export function currentResult(
  result: DecisionResult | null,
  resumeLost: boolean,
  stopped: boolean,
  status: ApprovalView["effective_status"],
): DecisionResult | null {
  return resumeLost && (stopped || status === "consumed") ? null : result;
}

export function ApprovalDetail({ approval, origins }: { approval: ApprovalView; origins: readonly Origin[] }) {
  const { request, action } = approval;
  const facts = actionFacts(action);
  const { scope } = request;
  return (
    <>
      <Section title="Exact action" origins={origins}>
        <p className="lead">{request.summary}</p>
        <Facts
          facts={[
            { term: "Kind", detail: <Mono>{request.action_kind}</Mono> },
            { term: "Status", detail: <Mono>{approval.effective_status}</Mono> },
            { term: "Action digest", detail: <Digest value={request.action_digest} /> },
            { term: "Adapter", detail: facts.adapter === null ? <span className="muted">not in the action</span> : <Mono>{facts.adapter}</Mono> },
            {
              term: "Execution mode",
              detail:
                facts.mode === null ? (
                  <span className="muted">not in the action</span>
                ) : (
                  <span data-testid="execution-mode">
                    <Mono>{facts.mode}</Mono>, guarded <Mono>{String(facts.guarded)}</Mono>
                  </span>
                ),
            },
            {
              term: "Fixture digest",
              detail: facts.fixtureSha256 === null ? <span className="muted">not in the action</span> : <Digest value={facts.fixtureSha256} />,
            },
            {
              term: "Evidence",
              detail: (
                <span>
                  <Mono>{request.payload.id}</Mono> <span className="muted">({request.payload.kind})</span>
                </span>
              ),
            },
            {
              term: "Evidence digest",
              detail:
                approval.payload_artifact_sha256 === null ? (
                  <span data-testid="evidence-unreadable">No stored evidence. Do not approve what you cannot read.</span>
                ) : (
                  <Digest value={approval.payload_artifact_sha256} />
                ),
            },
          ]}
        />
        <h3>Action the digest binds</h3>
        {action === null ? (
          <Notice state="failed" title="The action could not be shown">
            The service did not return the action for this digest. Do not approve what you cannot read.
          </Notice>
        ) : (
          <pre className="action-json" tabIndex={0} aria-label="Exact action" data-testid="exact-action">
            {prettyJson(action)}
          </pre>
        )}
      </Section>
      <Section title="Scope" origins={origins}>
        <Facts
          facts={[
            { term: "Project", detail: <Mono>{scope.project_id}</Mono> },
            { term: "Principal", detail: <Mono>{scope.principal.principal_id}</Mono> },
            { term: "Role", detail: <Mono>{scope.role}</Mono> },
            { term: "Delegated by", detail: scope.delegated_by === null ? <span className="muted">none</span> : <Mono>{scope.delegated_by}</Mono> },
            { term: "Allowed tools", detail: scope.allowed_tools.length === 0 ? <span className="muted">none</span> : <Mono>{scope.allowed_tools.join(", ")}</Mono> },
            { term: "Policy", detail: <Mono>{request.policy_version}</Mono> },
            { term: "Requested by", detail: <Mono>{request.requested_by_role}</Mono> },
            { term: "Expires", detail: formatTimestamp(request.expires_at) },
          ]}
        />
      </Section>
      <Section title="Material risks" origins={origins}>
        <ul className="plain" data-testid="material-risks">
          {request.material_risks.map((risk, index) => (
            <li key={index}>{risk}</li>
          ))}
        </ul>
      </Section>
    </>
  );
}

export function DecisionPanel({
  approval,
  session,
  busy,
  busyAction = null,
  failure,
  result,
  origins,
  unspent = false,
  blocked = false,
  stoppedText = null,
  onDecide,
  onResume,
  resumeLost = false,
}: {
  approval: ApprovalView;
  session: SessionInfo | null;
  busy: boolean;
  busyAction?: "decide" | "resume" | null;
  failure: Failure | null;
  result: DecisionResult | null;
  origins: readonly Origin[];
  unspent?: boolean;
  blocked?: boolean;
  stoppedText?: string | null;
  onDecide: (decision: Decision, reason: string) => void;
  onResume: () => void;
  resumeLost?: boolean;
}) {
  const [reason, setReason] = useState("");
  const gate = decisionGate(approval, session);
  const canSubmit = gate.open && approval.action !== null && !busy;
  const preview = buildSubmission(approval, "approve", reason);
  const refused = result !== null && !result.resumed;
  const resumeFailure = refused && result.resume_error !== null
    ? describeFailure({ kind: "service", status: 409, ...result.resume_error })
    : null;
  const canResume = resumeFailure?.reason !== CAMPAIGN_FINISHED;
  return (
    <Section title="Decision" origins={origins}>
      {stoppedText !== null && !blocked && !refused ? (
        <Notice state="conflict" title="This campaign has stopped">
          <p>{stoppedText}</p>
        </Notice>
      ) : null}
      {approval.decision === null ? null : (
        <Facts
          facts={[
            { term: "Decision", detail: <Tag tone="neutral">{approval.decision.decision}</Tag> },
            { term: "Approver (from the session)", detail: <Mono>{approval.decision.approver.principal_id}</Mono> },
            { term: "Decided", detail: formatTimestamp(approval.decision.decided_at) },
            { term: "Reason", detail: approval.decision.reason ?? <span className="muted">none given</span> },
          ]}
        />
      )}
      {gate.note === null ? null : <p>{gate.note}</p>}
      {busy ? (
        <BusyNote>
          {busyAction === "resume"
            ? "Resuming the campaign…"
            : busyAction === "decide"
              ? "Recording your decision…"
              : "Working…"}
        </BusyNote>
      ) : null}
      {gate.open ? (
        <form
          className="stack-form"
          onSubmit={(event) => {
            event.preventDefault();
          }}
        >
          <label htmlFor="decision-reason">Reason (optional)</label>
          <textarea
            id="decision-reason"
            rows={2}
            maxLength={2000}
            value={reason}
            disabled={busy}
            onChange={(event) => setReason(event.currentTarget.value)}
            aria-describedby="decision-reason-count"
          />
          <p className="char-count" id="decision-reason-count" data-near-limit={2000 - reason.length < 200 ? "true" : "false"}>
            {2000 - reason.length} characters left
          </p>
          <div className="actions">
            <button type="button" disabled={!canSubmit} onClick={() => onDecide("approve", reason)}>
              Approve this action
            </button>
            <button type="button" className="quiet" disabled={!canSubmit} onClick={() => onDecide("reject", reason)}>
              Reject
            </button>
          </div>
          <p className="muted">
            Your decision applies only to digest <Mono>{preview.action_digest.slice(0, 12)}</Mono>. The service takes the
            approver from your session and decides whether you may approve.
          </p>
        </form>
      ) : null}
      {/* A second refusal of the same resume would only repeat the notice below. A lost
          decide response is moot once the service's own record shows the decision; a lost
          resume is not, until the campaign shows that it landed. */}
      {failure === null ||
      (refused && failureState(failure) === "blocked") ||
      (failure.kind === "transport" &&
        approval.decision !== null &&
        !(resumeLost && approval.effective_status !== "consumed")) ? null : (
        <FailureNotice failure={failure} />
      )}
      {refused ? (
        <Notice
          state={resumeFailure?.state === "blocked" ? "blocked" : "conflict"}
          title="Decision recorded, campaign not resumed"
          live
        >
          <p>{resumeFailure?.message ?? "The service gave no reason."}</p>
          {resumeFailure?.help === null || resumeFailure?.help === undefined ? null : <p>{resumeFailure.help}</p>}
          {resumeFailure?.reason ? (
            <p className="muted">
              Reason <Mono>{resumeFailure.reason}</Mono>
            </p>
          ) : null}
          {canResume ? (
            <button type="button" disabled={busy} onClick={onResume}>
              Resume campaign
            </button>
          ) : (
            <p>The campaign has stopped, so this decision moved nothing and nothing ran.</p>
          )}
        </Notice>
      ) : blocked ? (
        <Notice state="blocked" title="Blocked: settings changed" live>
          <p>{BLOCKED_ENDED_HELP}</p>
        </Notice>
      ) : unspent ? (
        <Notice state="conflict" title="Approved, not yet used">
          <p>The decision is recorded, but the campaign has not resumed, so the approval is unspent.</p>
          <button type="button" disabled={busy} onClick={onResume}>
            Resume campaign
          </button>
        </Notice>
      ) : null}
    </Section>
  );
}
