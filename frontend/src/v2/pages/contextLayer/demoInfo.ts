import { api, type ContextLayerDemoInfo } from "../../../lib/api";

/**
 * `GET /api/context-layer/demo-account`, read once per workspace for every Chat
 * consent card on the page (a thread can hold several), and again after
 * `DEMO_INFO_TTL_MS` so a demo account an admin just configured shows up. A failed
 * read is not kept: the next card asks again.
 */
export const DEMO_INFO_TTL_MS = 5 * 60 * 1000;

const cache = new Map<string, { at: number; info: Promise<ContextLayerDemoInfo> }>();

export function loadDemoInfo(
  workspaceId: string,
  fetcher: () => Promise<ContextLayerDemoInfo> = api.getContextLayerDemoAccount,
  now: () => number = Date.now,
): Promise<ContextLayerDemoInfo> {
  const hit = cache.get(workspaceId);
  if (hit && now() - hit.at < DEMO_INFO_TTL_MS) return hit.info;
  const info = fetcher();
  cache.set(workspaceId, { at: now(), info });
  info.catch(() => {
    if (cache.get(workspaceId)?.info === info) cache.delete(workspaceId);
  });
  return info;
}

/** Drop every cached read (tests). */
export function resetDemoInfo(): void {
  cache.clear();
}
