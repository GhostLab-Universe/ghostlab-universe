import { useEffect, useRef } from "react";
import type { CampaignView, SessionInfo } from "../api/api.generated.ts";
import type { FixtureHarness } from "../data/fixtureSource.ts";
import type { DataSource } from "../data/source.ts";
import type { LiveState } from "../logic/live.ts";
import { looksExpired } from "../logic/approval.ts";
import { campaignHref, VIEWS, type Route } from "../logic/routes.ts";
import { campaignStatus, inferenceLabel } from "../logic/status.ts";
import { HomePage } from "../views/HomePage.tsx";
import { ExtensionsPage } from "../views/ExtensionsPage.tsx";
import { ApprovalPage } from "../views/ApprovalPage.tsx";
import { CampaignPanel } from "../views/CampaignPage.tsx";
import { CapsulePage } from "../views/CapsulePage.tsx";
import { DiffPage } from "../views/DiffPage.tsx";
import { EvidencePage } from "../views/EvidencePage.tsx";
import { FailureNotice, Loading, Mono, Notice, OriginNote, Tag } from "./components.tsx";
import { useCampaignLive, useResource, useRoute } from "./hooks.ts";

export function App({ source, harness }: { source: DataSource; harness: FixtureHarness | null }) {
  const route = useRoute();
  const { resource: session } = useResource("session", 0, () => source.getSession());
  const main = useRef<HTMLElement | null>(null);
  const sessionInfo = session.status === "ready" ? session.data : null;

  return (
    <>
      <a
        className="skip"
        href="#main"
        onClick={(event) => {
          event.preventDefault();
          main.current?.focus();
        }}
      >
        Skip to content
      </a>
      {source.kind === "fixture" ? <TestDataBanner /> : null}
      <header className="masthead">
        <a className="brand" href="#/">
          GhostLab<span> Universe</span>
        </a>
        <nav className="topnav" aria-label="Sections">
          <a href="#/">Campaigns</a> <a href="#/extensions">Extensions</a>
        </nav>
        <SessionFacts session={sessionInfo} />
      </header>
      {sessionInfo !== null && source.kind === "api" ? <ServiceModeNotice session={sessionInfo} /> : null}
      {session.status === "failed" ? (
        <div className="page">
          <FailureNotice failure={session.failure} />
        </div>
      ) : null}
      <main id="main" ref={main} tabIndex={-1} className="page">
        {route.page === "extensions" ? (
          <ExtensionsPage source={source} />
        ) : route.page === "home" ? (
          <HomePage source={source} harness={harness} session={sessionInfo} />
        ) : (
          <CampaignPage key={route.campaignId} source={source} harness={harness} route={route} session={sessionInfo} />
        )}
      </main>
    </>
  );
}

export function TestDataBanner() {
  return (
    <div className="test-banner" role="note" data-testid="test-data-banner">
      <strong>Test data.</strong> Nothing on this page came from a running service. It is not a live or integrated
      result. Worlds are synthetic.
    </div>
  );
}

export function SessionFacts({ session }: { session: SessionInfo | null }) {
  if (session === null) return null;
  return (
    <p className="session">
      <Mono>{session.principal.principal_id}</Mono> in <Mono>{session.project_id}</Mono>{" "}
      <span className="muted">
        <Mono>auth_mode</Mono> {session.auth_mode}, <Mono>production_authenticated</Mono>{" "}
        {String(session.production_authenticated)}, <Mono>inference</Mono> {session.inference}
      </span>
    </p>
  );
}

/** What this service can actually do. "fixture" inference is scripted, never a model. */
export function ServiceModeNotice({ session }: { session: SessionInfo }) {
  if (session.inference === "live") return null;
  const scripted = session.inference === "fixture" || session.inference === "scripted";
  return (
    <div className="service-mode" role="note">
      {scripted
        ? "This service's inference is scripted test data, not a model."
        : session.inference === "disabled"
          ? "Inference is disabled on this service, so it cannot start new campaigns."
          : "This service's inference mode is not recorded."}
    </div>
  );
}

type CampaignRoute = Extract<Route, { page: "campaign" }>;

function CampaignPage({
  source,
  harness,
  route,
  session,
}: {
  source: DataSource;
  harness: FixtureHarness | null;
  route: CampaignRoute;
  session: SessionInfo | null;
}) {
  const { live, state } = useCampaignLive(source, route.campaignId);
  const heading = useRef<HTMLHeadingElement | null>(null);
  const routeKey = `${route.view}/${route.run ?? ""}/${route.eventId ?? ""}`;
  const firstRender = useRef(true);

  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    if (route.eventId === null) heading.current?.focus();
  }, [routeKey]);

  return (
    <>
      <CampaignStatusNotices state={state} harness={harness} live={live} />
      {state.view === null ? (
        <CampaignUnavailable state={state} campaignId={route.campaignId} />
      ) : (
        <>
          <CampaignHeading view={state.view} state={state} heading={heading} />
          <ViewNav view={state.view} route={route} />
          <RouteBody
            source={source}
            route={route}
            state={state}
            view={state.view}
            session={session}
            refresh={live.refresh}
          />
        </>
      )}
    </>
  );
}

function CampaignStatusNotices({
  state,
  harness,
  live,
}: {
  state: LiveState;
  harness: FixtureHarness | null;
  live: { refresh: () => Promise<void> };
}) {
  return (
    <>
      {state.connection === "reconnecting" ? (
        <Notice state="reconnecting" title="Reconnecting" live>
          {state.view === null
            ? "The service is not responding yet."
            : "This is the last status the service gave. New events are held until the status is read again."}
        </Notice>
      ) : null}
      {state.phase === "ready" && state.failure !== null ? <FailureNotice failure={state.failure} /> : null}
      {harness === null ? null : (
        <div className="test-controls" role="group" aria-label="Test controls">
          <span className="origin-flag">Test controls</span>
          <button
            type="button"
            onClick={() => {
              harness.setOffline(true);
              void live.refresh();
            }}
          >
            Drop connection
          </button>
          <button
            type="button"
            onClick={() => {
              harness.setOffline(false);
              void live.refresh();
            }}
          >
            Restore connection
          </button>
        </div>
      )}
    </>
  );
}

function CampaignUnavailable({ state, campaignId }: { state: LiveState; campaignId: string }) {
  if (state.phase === "failed" && state.failure !== null) {
    return (
      <>
        <h1>
          Campaign <Mono>{campaignId}</Mono>
        </h1>
        <FailureNotice failure={state.failure} />
        <p>
          <a href="#/">Back to campaigns</a>
        </p>
      </>
    );
  }
  return state.connection === "reconnecting" ? null : <Loading what="campaign" />;
}

function CampaignHeading({
  view,
  state,
  heading,
}: {
  view: CampaignView;
  state: LiveState;
  heading: { current: HTMLHeadingElement | null };
}) {
  const status = campaignStatus(view.record.status, view.outcome);
  return (
    <header className="campaign-head">
      <p className="eyebrow">
        <Mono>{view.record.spec.world_version}</Mono> <Mono>{view.record.campaign_id}</Mono>
      </p>
      <h1 ref={heading} tabIndex={-1}>
        {view.record.spec.objective}
      </h1>
      <p className="status-line" aria-live="polite">
        <Tag tone={status.tone}>{status.label}</Tag>
        <span className="muted" data-testid="synthetic-label">
          Synthetic world: no real people or money
        </span>
        <span className="muted" data-testid="campaign-inference">
          {inferenceLabel(view.inference)}
        </span>
        {view.outcome === null ? null : (
          <span>
            Outcome <Mono>{view.outcome}</Mono>
          </span>
        )}
      </p>
      <OriginNote origins={state.origins} />
    </header>
  );
}

function ViewNav({ view, route }: { view: CampaignView; route: CampaignRoute }) {
  return (
    <nav className="view-nav" aria-label="Campaign views">
      <ul>
        {VIEWS.map((item) => {
          const current = route.view === item.name;
          const waiting = item.name === "approval" && view.record.status === "awaiting_approval";
          const expired = waiting && looksExpired(view.approval?.expires_at, Date.now());
          return (
            <li key={item.name}>
              <a
                href={campaignHref(view.record.campaign_id, { view: item.name, experimentId: route.experimentId })}
                {...(current ? { "aria-current": "page" as const } : {})}
              >
                {item.label}
                {waiting ? <span className="nav-flag"> ({expired ? "approval expired" : "awaiting decision"})</span> : null}
              </a>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

function RouteBody({
  source,
  route,
  state,
  view,
  session,
  refresh,
}: {
  source: DataSource;
  route: CampaignRoute;
  state: LiveState;
  view: CampaignView;
  session: SessionInfo | null;
  refresh: () => Promise<void>;
}) {
  const experimentId = route.experimentId ?? view.experiments.at(-1)?.experiment_id ?? null;
  const common = { source, view, experimentId };
  switch (route.view) {
    case "overview":
      return (
        <CampaignPanel
          view={view}
          origins={state.origins}
          source={source}
          refresh={refresh}
          live={{ connection: state.connection, cursor: state.cursor }}
        />
      );
    case "evidence":
      return <EvidencePage {...common} route={route} events={state.events} eventOrigins={state.origins} />;
    case "diff":
      return <DiffPage {...common} />;
    case "approval":
      return <ApprovalPage source={source} view={view} session={session} refresh={refresh} />;
    case "capsule":
      return <CapsulePage {...common} />;
  }
}
