import type {
  WorldDivergence,
  WorldInvariantFinding,
  WorldRunResult,
  WorldTraceEvent,
} from "../api/api.generated.ts";
import { isErrorOutcome, violatingEventIds } from "./evidence.ts";

/**
 * Plain words for what a journey run did, read only from recorded events and their state.
 * The verifier decides what violated an invariant: its finding names the event. This only
 * says who and what that event was about. Anything not recorded yields null, not a guess.
 */
type Row = Record<string, unknown>;

function row(value: unknown): Row | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Row) : null;
}

function rows(value: unknown): Row[] {
  if (!Array.isArray(value)) return [];
  return (value as unknown[]).flatMap((item): Row[] => {
    const entry = row(item);
    return entry === null ? [] : [entry];
  });
}

export interface Delivery {
  eventId: number;
  messageId: string;
  recipient: string;
  /** The recipient's sharing in this event's recorded state. */
  sharing: boolean | null;
  /** Set when the world refused the delivery. */
  denied: string | null;
}

export function deliveryAt(event: WorldTraceEvent | null): Delivery | null {
  if (event === null || event.request.tool !== "deliver_message") return null;
  const messageId = event.request.arguments.message_id;
  if (typeof messageId !== "string") return null;
  const recipient = row(row(event.state.messages)?.[messageId])?.recipient_id;
  if (typeof recipient !== "string") return null;
  const sharing = row(event.state.sharing)?.[recipient];
  return {
    eventId: event.event_id,
    messageId,
    recipient,
    sharing: typeof sharing === "boolean" ? sharing : null,
    denied: isErrorOutcome(event.outcome) ? event.outcome.error : null,
  };
}

/** Participants whose sharing ended at exactly this event. */
export function sharingLostAt(event: WorldTraceEvent): string[] {
  return rows(event.state.permission_timeline)
    .filter((item) => item.from_event === event.event_id && item.sharing === false)
    .flatMap((item) => (typeof item.participant === "string" ? [item.participant] : []));
}

function lostAt(event: WorldTraceEvent, participant: string): number | null {
  const lost = rows(event.state.permission_timeline).filter(
    (item) => item.participant === participant && item.sharing === false && typeof item.from_event === "number",
  );
  const last = lost.at(-1)?.from_event;
  return typeof last === "number" ? last : null;
}

/** The last event before `before` where the run itself read this participant as sharing. */
function lastReadAllowed(run: WorldRunResult, participant: string, before: number): number | null {
  const reads = run.trace.filter((event) => {
    if (event.event_id >= before || isErrorOutcome(event.outcome)) return false;
    const seen = rows(row(event.outcome.result)?.participants).find((item) => item.participant_id === participant);
    return seen?.sharing === true;
  });
  return reads.at(-1)?.event_id ?? null;
}

export interface Leak extends Delivery {
  lostAtEvent: number | null;
  lastReadAllowedAt: number | null;
}

/** Deliveries the verifier cites where the recorded state shows sharing already gone. */
export function leaksIn(run: WorldRunResult | null, findings: readonly WorldInvariantFinding[]): Leak[] {
  if (run === null) return [];
  const cited = new Set(violatingEventIds(findings));
  return run.trace.flatMap((event) => {
    if (!cited.has(event.event_id)) return [];
    const delivery = deliveryAt(event);
    if (delivery === null || delivery.denied !== null || delivery.sharing !== false) return [];
    return [
      {
        ...delivery,
        lostAtEvent: lostAt(event, delivery.recipient),
        lastReadAllowedAt: lastReadAllowed(run, delivery.recipient, event.event_id),
      },
    ];
  });
}

export function leakSentence(leak: Leak): string {
  const lost = leak.lostAtEvent === null ? `${leak.recipient} had lost sharing` : `${leak.recipient} lost sharing at event ${leak.lostAtEvent}`;
  return `${lost}; ${leak.messageId} was delivered to them at event ${leak.eventId}.`;
}

/** What makes an apparently careful agent's leak a race, when the run shows it. */
export function checkSentence(leak: Leak): string | null {
  if (leak.lastReadAllowedAt === null) return null;
  return `The run last read their sharing as allowed at event ${leak.lastReadAllowedAt}, so it delivered on a stale check.`;
}

/** Sharing the fork lost at the first divergence. The baseline has no such row. */
export function divergenceCause(divergence: WorldDivergence): string | null {
  const event = divergence.faulted;
  if (event === null) return null;
  const inBaseline = new Set(rows(divergence.baseline?.state.permission_timeline).map((item) => JSON.stringify(item)));
  const lost = rows(event.state.permission_timeline).filter(
    (item) => item.sharing === false && !inBaseline.has(JSON.stringify(item)) && typeof item.participant === "string",
  );
  const participant = lost.at(-1)?.participant;
  if (typeof participant !== "string") return null;
  const cause = `The worlds separate because ${participant} lost sharing at event ${event.event_id}.`;
  const delivery = deliveryAt(event);
  if (delivery === null) return cause;
  if (delivery.recipient === participant) {
    return `${cause} The same event delivered ${delivery.messageId} to them.`;
  }
  return `${cause} That event's delivery of ${delivery.messageId} went to ${delivery.recipient}, who still had sharing, and is not the violation.`;
}

export function deliveryLabel(delivery: Delivery): string {
  return `${delivery.messageId} to ${delivery.recipient}`;
}
