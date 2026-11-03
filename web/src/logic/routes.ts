import type { RunName } from "../data/source.ts";

export type ViewName = "overview" | "evidence" | "diff" | "approval" | "capsule";

export const VIEWS: ReadonlyArray<{ name: ViewName; label: string }> = [
  { name: "overview", label: "Campaign" },
  { name: "evidence", label: "Evidence" },
  { name: "diff", label: "Reality Diff" },
  { name: "approval", label: "Approval" },
  { name: "capsule", label: "Capsule" },
];

export type Route =
  | { page: "home" }
  | { page: "extensions" }
  | { page: "account" }
  | {
      page: "campaign";
      campaignId: string;
      view: ViewName;
      experimentId: string | null;
      run: RunName | null;
      eventId: number | null;
    };

const VIEW_NAMES = new Set<string>(VIEWS.map((view) => view.name));
const RUN_NAMES = new Set<string>(["baseline", "fork", "repaired"]);

export function parseRoute(hash: string): Route {
  const [path = "", search = ""] = hash.replace(/^#\/?/, "").split("?");
  const [section, rawId, rawView] = path.split("/");
  if (section === "account") return { page: "account" };
  if (section === "extensions") return { page: "extensions" };
  if (section !== "campaign" || rawId === undefined || rawId === "") return { page: "home" };
  let campaignId: string;
  try {
    campaignId = decodeURIComponent(rawId);
  } catch {
    return { page: "home" };
  }
  const params = new URLSearchParams(search);
  const run = params.get("run");
  const event = Number(params.get("event"));
  return {
    page: "campaign",
    campaignId,
    view: rawView !== undefined && VIEW_NAMES.has(rawView) ? (rawView as ViewName) : "overview",
    experimentId: params.get("experiment"),
    run: run !== null && RUN_NAMES.has(run) ? (run as RunName) : null,
    eventId: Number.isInteger(event) && event > 0 ? event : null,
  };
}

export interface CampaignLink {
  view?: ViewName;
  experimentId?: string | null;
  run?: RunName | null;
  eventId?: number | null;
}

export function campaignHref(campaignId: string, link: CampaignLink = {}): string {
  const params = new URLSearchParams();
  if (link.experimentId) params.set("experiment", link.experimentId);
  if (link.run) params.set("run", link.run);
  if (link.eventId) params.set("event", String(link.eventId));
  const query = params.size > 0 ? `?${params}` : "";
  return `#/campaign/${encodeURIComponent(campaignId)}/${link.view ?? "overview"}${query}`;
}
export function extensionsHref(): string {
  return "#/extensions";
}
