import type {
  ApprovalSubmission,
  ApprovalView,
  CampaignView,
  CapsuleInfo,
  DecisionResult,
  ErrorInfo,
  EventPage,
  ExperimentView,
  ReplayResult,
  RunView,
  SessionInfo,
  WorldCampaignSpec,
} from "../api/api.generated.ts";

export type RunName = "baseline" | "fork" | "repaired";

/** Where a value on screen came from. Anything but "service" is test data. */
export type Origin =
  | { kind: "service" }
  | { kind: "published_example" | "recorded_core_output" | "hand_authored"; source: string; note: string | null };

export type Failure =
  | { kind: "service"; status: number; error: ErrorInfo; reason: string | null }
  | { kind: "transport"; message: string }
  | { kind: "invalid_response"; operation: string; message: string };

export type Result<T> =
  | { ok: true; data: T; origins: readonly Origin[] }
  | { ok: false; failure: Failure };

/**
 * Everything the UI reads or does, named by the data rather than by URL. The API client
 * implements it over M07's contract; the fixture source implements it over test data.
 */
export interface DataSource {
  readonly kind: "api" | "fixture";
  getSession(): Promise<Result<SessionInfo>>;
  createCampaign(spec: WorldCampaignSpec, idempotencyKey: string, scriptedScenario?: string): Promise<Result<CampaignView>>;
  getCampaign(campaignId: string): Promise<Result<CampaignView>>;
  listEvents(campaignId: string, after: number): Promise<Result<EventPage>>;
  cancelCampaign(campaignId: string): Promise<Result<CampaignView>>;
  resumeCampaign(campaignId: string): Promise<Result<CampaignView>>;
  getExperiment(campaignId: string, experimentId: string): Promise<Result<ExperimentView>>;
  getRun(campaignId: string, experimentId: string, run: RunName): Promise<Result<RunView>>;
  exportCapsule(campaignId: string, experimentId: string, run: RunName): Promise<Result<CapsuleInfo>>;
  replayCapsule(campaignId: string, experimentId: string, run: RunName): Promise<Result<ReplayResult>>;
  downloadArtifact(artifactId: string): Promise<Result<Blob>>;
  getApproval(approvalId: string): Promise<Result<ApprovalView>>;
  decideApproval(submission: ApprovalSubmission, idempotencyKey: string): Promise<Result<DecisionResult>>;
}

export const SERVICE_ORIGIN: readonly Origin[] = [{ kind: "service" }];

export function isTestData(origins: readonly Origin[]): boolean {
  return origins.some((origin) => origin.kind !== "service");
}
