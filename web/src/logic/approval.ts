import type { ApprovalSubmission, ApprovalView, SessionInfo } from "../api/api.generated.ts";

export type Decision = ApprovalSubmission["decision"];

/**
 * The only body this UI can send. The id and digest are copied from what the service
 * showed, and the approver, role and scope are not fields here: the session decides.
 */
export function buildSubmission(view: ApprovalView, decision: Decision, reason: string): ApprovalSubmission {
  const trimmed = reason.trim();
  return {
    schema_version: 1,
    approval_id: view.request.approval_id,
    action_digest: view.request.action_digest,
    decision,
    reason: trimmed === "" ? null : trimmed,
  };
}

export interface DecisionGate {
  open: boolean;
  /** Why the controls are closed. A convenience: the service still decides. */
  note: string | null;
}

export function decisionGate(view: ApprovalView, session: SessionInfo | null): DecisionGate {
  if (view.effective_status !== "pending") {
    return { open: false, note: `This approval is ${view.effective_status}.` };
  }
  if (session !== null && !session.can_approve) {
    return { open: false, note: "This session cannot approve. The service would refuse the decision." };
  }
  return { open: true, note: null };
}

/**
 * What to say on a stopped campaign, only while the approval is still undecided or really
 * is unspent. A used or rejected approval has nothing left to warn about.
 */
export function stoppedNote(
  effectiveStatus: ApprovalView["effective_status"],
  stopped: boolean,
  repairRan: boolean,
): string | null {
  if (!stopped) return null;
  if (effectiveStatus === "pending") {
    return "A decision is still recorded, but it can't run anything. The approval stays unspent.";
  }
  if (effectiveStatus === "approved" && !repairRan) return "The approval was never used, and it can't run anything now.";
  return null;
}

/** Wording only: the service decides whether an approval has expired. */
export function looksExpired(expiresAt: string | undefined, now: number): boolean {
  return expiresAt !== undefined && Date.parse(expiresAt) <= now;
}

export interface ActionFacts {
  adapter: string | null;
  mode: string | null;
  guarded: boolean | null;
  fixtureSha256: string | null;
}

/** Reads the bound action for display. A missing field stays null rather than a default. */
export function actionFacts(action: ApprovalView["action"]): ActionFacts {
  const execution = action?.execution;
  const recorded = typeof execution === "object" && execution !== null ? (execution as Record<string, unknown>) : null;
  const text = (value: unknown) => (typeof value === "string" ? value : null);
  return {
    adapter: text(action?.adapter),
    mode: text(recorded?.mode),
    guarded: typeof recorded?.guarded === "boolean" ? recorded.guarded : null,
    fixtureSha256: text(action?.fixture_sha256),
  };
}

/**
 * Keeps one Idempotency-Key per exact body, so retrying after a lost response can't
 * record a second decision, and changing the decision or reason is a new action.
 */
export class IdempotencyKeys {
  #keys = new Map<string, string>();
  #mint: () => string;

  constructor(mint: () => string = () => `decision-${crypto.randomUUID()}`) {
    this.#mint = mint;
  }

  keyFor(submission: ApprovalSubmission): string {
    const body = JSON.stringify(submission);
    const existing = this.#keys.get(body);
    if (existing !== undefined) return existing;
    const key = this.#mint();
    this.#keys.set(body, key);
    return key;
  }
}
