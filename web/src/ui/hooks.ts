import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { DataSource, Failure, Origin, Result } from "../data/source.ts";
import { CampaignLive, type LiveState } from "../logic/live.ts";
import { parseRoute, type Route } from "../logic/routes.ts";

export type Resource<T> =
  | { status: "loading" }
  | { status: "ready"; data: T; origins: readonly Origin[] }
  | { status: "failed"; failure: Failure };

/**
 * Loads one thing. A new `key` is a different thing, so it shows loading; a new
 * `version` re-reads the same thing in place. Only the newest response is applied.
 */
export function useResource<T>(
  key: string | null,
  version: unknown,
  load: () => Promise<Result<T>>,
): { resource: Resource<T>; reload: () => void } {
  const [resource, setResource] = useState<Resource<T>>({ status: "loading" });
  const [tick, setTick] = useState(0);
  const latest = useRef(0);
  const loadedKey = useRef<string | null>(null);

  useEffect(() => {
    if (key === null) return;
    const request = (latest.current += 1);
    if (loadedKey.current !== key) setResource({ status: "loading" });
    loadedKey.current = key;
    void load().then((result) => {
      if (request !== latest.current) return;
      setResource(
        result.ok
          ? { status: "ready", data: result.data, origins: result.origins }
          : { status: "failed", failure: result.failure },
      );
    });
    // `load` closes over what `key` and `version` identify.
  }, [key, version, tick]);

  return { resource, reload: () => setTick((value) => value + 1) };
}

export function useRoute(): Route {
  const hash = useSyncExternalStore(
    (onChange) => {
      window.addEventListener("hashchange", onChange);
      return () => window.removeEventListener("hashchange", onChange);
    },
    () => window.location.hash,
    () => "",
  );
  return useMemo(() => parseRoute(hash), [hash]);
}

export function useCampaignLive(source: DataSource, campaignId: string): { live: CampaignLive; state: LiveState } {
  const live = useMemo(() => new CampaignLive(source, campaignId), [source, campaignId]);
  useEffect(() => {
    live.start();
    return () => live.stop();
  }, [live]);
  const state = useSyncExternalStore(live.subscribe, live.getState, live.getState);
  return { live, state };
}
