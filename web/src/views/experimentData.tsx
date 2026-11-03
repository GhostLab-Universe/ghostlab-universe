import type { ReactNode } from "react";
import type { CampaignView, Coverage, ExperimentView, RepairView } from "../api/api.generated.ts";
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

/** A repaired run can pass every check it met and still have missed a fault. */
export function LostFaultsNotice({ repair }: { repair: RepairView | null }) {
  const lost = repair?.lost_faults ?? [];
  if (lost.length === 0) return null;
  return (
    <Notice state="incomplete" title="This repair missed a fault" live>
      The repaired run never met <Mono>{lost.join(", ")}</Mono>, which the fork met. Its passing checks prove nothing about
      that fault.
    </Notice>
  );
}

export function CoverageLine({ coverage, recorded }: { coverage: Coverage; recorded: boolean }) {
  return (
    <p data-testid="coverage">
      Coverage: baseline <Mono>{coverage.baseline}</Mono>, fork <Mono>{coverage.fork}</Mono>, faults_triggered{" "}
      <Mono>{coverage.faults_triggered === null ? "not recorded" : String(coverage.faults_triggered)}</Mono>, complete{" "}
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
