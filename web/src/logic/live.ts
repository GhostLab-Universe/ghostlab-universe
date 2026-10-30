import type { CampaignView, EventPage, ExperimentEvent } from "../api/api.generated.ts";
import type { DataSource, Failure, Origin } from "../data/source.ts";
import { failureState, revokesAccess } from "./failure.ts";
import { isTerminal } from "./status.ts";

/**
 * The campaign's on-screen state. Two rules keep it honest:
 *
 * Status comes only from an authoritative campaign read. ExperimentEvent has no status
 * field and its summary is untrusted text, so an event can never change what the
 * campaign is shown to be.
 *
 * Every read is numbered. A response older than the newest applied, or sent before the
 * connection dropped, is ignored. Reading resumes from the cursor, so nothing is lost.
 */
export interface LiveState {
  phase: "loading" | "ready" | "failed";
  view: CampaignView | null;
  origins: readonly Origin[];
  events: readonly ExperimentEvent[];
  cursor: number;
  connection: "live" | "reconnecting";
  failure: Failure | null;
  appliedRead: number;
  /** Responses to reads numbered at or below this were sent before the outage. */
  floor: number;
}

export const INITIAL_LIVE: LiveState = {
  phase: "loading",
  view: null,
  origins: [],
  events: [],
  cursor: 0,
  connection: "live",
  failure: null,
  appliedRead: 0,
  floor: 0,
};

export type LiveAction =
  | { type: "snapshot"; read: number; view: CampaignView; origins: readonly Origin[] }
  | { type: "events"; read: number; page: EventPage }
  | { type: "failure"; read: number; failure: Failure; latestRead: number };

/** event_count only grows, so a lower one is an older checkpoint whatever the clock says. */
function isOlder(incoming: CampaignView, current: CampaignView): boolean {
  if (incoming.event_count !== current.event_count) return incoming.event_count < current.event_count;
  return Date.parse(incoming.record.updated_at) < Date.parse(current.record.updated_at);
}

function mergeEvents(state: LiveState, page: EventPage): LiveState {
  const campaignId = state.view?.record.campaign_id;
  const fresh = page.events
    .filter((event) => event.sequence > state.cursor && event.campaign_id === campaignId)
    .sort((a, b) => a.sequence - b.sequence);
  return {
    ...state,
    events: [...state.events, ...fresh],
    cursor: Math.max(state.cursor, page.next_after, ...fresh.map((event) => event.sequence)),
  };
}

export function liveReducer(state: LiveState, action: LiveAction): LiveState {
  switch (action.type) {
    case "snapshot": {
      if (action.read <= state.floor || action.read <= state.appliedRead) return state;
      const older = state.view !== null && isOlder(action.view, state.view);
      return {
        ...state,
        phase: "ready",
        view: older ? state.view : action.view,
        origins: older ? state.origins : action.origins,
        connection: "live",
        failure: null,
        appliedRead: action.read,
      };
    }
    case "events":
      // Before the authoritative read lands, the cursor stays put and these are re-read.
      if (action.read <= state.floor || state.connection === "reconnecting") return state;
      return mergeEvents(state, action.page);
    case "failure": {
      if (action.read <= state.floor) return state;
      if (failureState(action.failure) === "reconnecting") {
        return { ...state, connection: "reconnecting", floor: action.latestRead };
      }
      if (state.view === null || revokesAccess(action.failure)) {
        return {
          ...state,
          phase: "failed",
          view: null,
          events: [],
          origins: [],
          connection: "live",
          failure: action.failure,
        };
      }
      return { ...state, failure: action.failure };
    }
  }
}

export interface LiveOptions {
  pollMs?: number;
  maxBackoffMs?: number;
  setTimer?: (run: () => void, ms: number) => unknown;
  clearTimer?: (timer: unknown) => void;
}

const MAX_EVENT_PAGES = 50;

/** Polls M07: the campaign first, then any events it says exist. Reads only. */
export class CampaignLive {
  #source: DataSource;
  #campaignId: string;
  #state = INITIAL_LIVE;
  #listeners = new Set<() => void>();
  #reads = 0;
  #timer: unknown = null;
  #running = false;
  #stopped = true;
  #pollMs: number;
  #maxBackoffMs: number;
  #backoffMs: number;
  #setTimer: (run: () => void, ms: number) => unknown;
  #clearTimer: (timer: unknown) => void;

  constructor(source: DataSource, campaignId: string, options: LiveOptions = {}) {
    this.#source = source;
    this.#campaignId = campaignId;
    this.#pollMs = options.pollMs ?? 2000;
    this.#maxBackoffMs = options.maxBackoffMs ?? 15000;
    this.#backoffMs = this.#pollMs;
    this.#setTimer = options.setTimer ?? ((run, ms) => setTimeout(run, ms));
    this.#clearTimer = options.clearTimer ?? ((timer) => clearTimeout(timer as ReturnType<typeof setTimeout>));
  }

  getState = (): LiveState => this.#state;

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  start(): void {
    this.#stopped = false;
    void this.#loop();
  }

  stop(): void {
    this.#stopped = true;
    this.#clearTimer(this.#timer);
  }

  /** Authoritative re-read, for after an action. Also restarts a finished poll loop. */
  refresh = async (): Promise<void> => {
    await this.#read();
    if (this.#shouldPoll()) this.#schedule(this.#pollMs);
  };

  #dispatch(action: LiveAction): void {
    this.#state = liveReducer(this.#state, action);
    for (const listener of this.#listeners) listener();
  }

  async #read(): Promise<void> {
    const read = (this.#reads += 1);
    const campaign = await this.#source.getCampaign(this.#campaignId);
    if (!campaign.ok) {
      this.#dispatch({ type: "failure", read, failure: campaign.failure, latestRead: this.#reads });
      return;
    }
    this.#dispatch({ type: "snapshot", read, view: campaign.data, origins: campaign.origins });
    await this.#drainEvents(campaign.data.event_count);
  }

  async #drainEvents(eventCount: number): Promise<void> {
    for (let page = 0; page < MAX_EVENT_PAGES && this.#state.cursor < eventCount; page += 1) {
      const read = (this.#reads += 1);
      const before = this.#state.cursor;
      const result = await this.#source.listEvents(this.#campaignId, before);
      if (!result.ok) {
        this.#dispatch({ type: "failure", read, failure: result.failure, latestRead: this.#reads });
        return;
      }
      this.#dispatch({ type: "events", read, page: result.data });
      if (!result.data.has_more || this.#state.cursor === before) return;
    }
  }

  #shouldPoll(): boolean {
    const { phase, view, connection, cursor } = this.#state;
    if (phase === "failed") return false;
    if (connection === "reconnecting" || view === null) return true;
    return !isTerminal(view.record.status) || view.job.state === "active" || cursor < view.event_count;
  }

  #schedule(ms: number): void {
    this.#clearTimer(this.#timer);
    if (!this.#stopped) this.#timer = this.#setTimer(() => void this.#loop(), ms);
  }

  async #loop(): Promise<void> {
    if (this.#running || this.#stopped) return;
    this.#running = true;
    await this.#read();
    this.#running = false;
    if (!this.#shouldPoll()) return;
    const reconnecting = this.#state.connection === "reconnecting";
    this.#backoffMs = reconnecting ? Math.min(this.#backoffMs * 2, this.#maxBackoffMs) : this.#pollMs;
    this.#schedule(this.#backoffMs);
  }
}
