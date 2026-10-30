import type { ReactNode } from "react";
import type { CampaignView, ExperimentView } from "../api/api.generated.ts";
import type { Coverage } from "../api/pending.ts";
import type { DataSource, Origin, RunName } from "../data/source.ts";
import { coverageNote } from "../logic/coverage.ts";
import { campaignHref, type ViewName } from "../logic/routes.ts";
import { FailureNotice, Loading, Mono, Notice } from "../ui/components.tsx";
import { useResource, type Resource } from "../ui/hooks.ts";

export function useExperiment(source: DataSource, view: CampaignView, experimentId: string | null) {
  const campaignId = view.record.campaign_id;
  return useResource<ExperimentView>(
    experimentId === null ? null : `${campaignId}/${experimentId}`,
    view.event_count,
    () => source.getExperiment(campaignId, experimentId ?? ""),
  );
}

export function useRun(
  source: DataSource,
  view: CampaignView,
  experimentId: string | null,
  run: RunName,
  available: boolean,
) {
  const campaignId = view.record.campaign_id;
  return useResource(
    experimentId === null || !available ? null : `${campaignId}/${experimentId}/${run}`,
    view.event_count,
    () => source.getRun(campaignId, experimentId ?? "", run),
  );
}

export function ExperimentGate({
  experimentId,
  resource,
  children,
}: {
  experimentId: string | null;
  resource: Resource<ExperimentView>;
  children: (experiment: ExperimentView, origins: readonly Origin[]) => ReactNode;
}) {
  if (experimentId === null) {
    return (
      <Notice state="empty" title="No experiments yet">
        Evidence appears here after the first experiment runs.
      </Notice>
    );
  }
  if (resource.status === "loading") return <Loading what="experiment" />;
  if (resource.status === "failed") return <FailureNotice failure={resource.failure} />;
  return <>{children(resource.data, resource.origins)}</>;
}

/** Warns about a truncated experiment. A complete one needs no notice. */
export function CoverageNotice({ coverage, violations }: { coverage: Coverage; violations: number }) {
  const note = coverageNote(coverage, violations);
  if (note === null) return null;
  return (
    <Notice state="incomplete" title={note.headline} live>
      {note.detail}
    </Notice>
  );
}

export function CoverageLine({ coverage, recorded }: { coverage: Coverage; recorded: boolean }) {
  return (
    <p data-testid="coverage">
      Coverage: baseline <Mono>{coverage.baseline}</Mono>, fork <Mono>{coverage.fork}</Mono>, complete{" "}
      <Mono>{String(coverage.complete)}</Mono>
      {recorded ? null : <span className="muted"> (read from the two recorded runs)</span>}
    </p>
  );
}

/** Only shown when a campaign has more than one experiment. */
export function ExperimentPicker({
  view,
  current,
  target,
}: {
  view: CampaignView;
  current: string;
  target: ViewName;
}) {
  if (view.experiments.length < 2) return null;
  return (
    <nav className="sub-nav" aria-label="Experiments">
      <ul>
        {view.experiments.map((experiment) => (
          <li key={experiment.experiment_id}>
            <a
              href={campaignHref(view.record.campaign_id, { view: target, experimentId: experiment.experiment_id })}
              {...(experiment.experiment_id === current ? { "aria-current": "page" as const } : {})}
            >
              Iteration {experiment.iteration}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}
