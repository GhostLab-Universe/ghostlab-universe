import type {
  ApprovalSubmission,
  CampaignRequest,
  ApprovalView,
  CampaignView,
  CapsuleInfo,
  DecisionResult,
  ErrorInfo,
  EventPage,
  ExtCampaignReport,
  ExtEnvironmentDetail,
  ExtEnvironmentList,
  ExtFidelityReport,
  ExtFixture,
  ExtInspection,
  ExtMinimizeResult,
  ExtQualificationReport,
  ExtReplayResult,
  ExtRetestResult,
  FidelityRequest,
  ExperimentView,
  InspectRequest,
  MinimizeRequest,
  QualifyRequest,
  ReplayRequest,
  ReplayResult,
  RetestRequest,
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
  listExtEnvironments(): Promise<Result<ExtEnvironmentList>>;
  getExtEnvironment(world: string): Promise<Result<ExtEnvironmentDetail>>;
  getExtFixture(world: string, name: string): Promise<Result<ExtFixture>>;
  qualifyExtension(request: QualifyRequest): Promise<Result<ExtQualificationReport>>;
  assessExtFidelity(request: FidelityRequest): Promise<Result<ExtFidelityReport>>;
  runExtCampaign(request: CampaignRequest): Promise<Result<ExtCampaignReport>>;
  inspectExtCapsule(request: InspectRequest): Promise<Result<ExtInspection>>;
  replayExtRecorded(request: ReplayRequest): Promise<Result<ExtReplayResult>>;
  retestExtFresh(request: RetestRequest): Promise<Result<ExtRetestResult>>;
  minimizeExtFaults(request: MinimizeRequest): Promise<Result<ExtMinimizeResult>>;
}

export const SERVICE_ORIGIN: readonly Origin[] = [{ kind: "service" }];

export function isTestData(origins: readonly Origin[]): boolean {
  return origins.some((origin) => origin.kind !== "service");
}
