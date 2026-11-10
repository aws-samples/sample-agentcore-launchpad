import { useCallback, useEffect, useRef, useState } from "react";

export interface HealthInfo {
  status: string;
  version: string;
  region: string;
  account_id?: string;
  /** N13-obs10: the deployed commit — `sha` / `short` (8 chars) / `built_at` (ISO, UTC) /
   *  `source` (`env` | `file` | `dist_stamp`), every key null when the host carries no
   *  build stamp; absent on older backends. Lets a demo's `recorded_version` (E01-obs1) be
   *  checked against the running console. */
  build?: {
    sha: string | null; short: string | null; built_at: string | null; source: string | null;
    /** N17-obs466: the commit the SERVED console bundle was built from (dist stamp) — what
     *  `NewVersionBanner` compares this tab's bundle with; absent on older backends. */
    dist_sha?: string | null;
  };
}

/** "loading" = no probe has answered yet · "ok" = last probe 2xx · "down" = failed. */
export type HealthStatus = "loading" | "ok" | "down";

export interface HealthState {
  /** Last successful payload — kept through a later outage so the region /
   *  account chips do not blank while the backend restarts. */
  health: HealthInfo | null;
  status: HealthStatus;
  refresh: () => void;
}

/** Steady-state re-probe cadence; `online` / `focus` re-probe immediately. */
export const HEALTH_PROBE_MS = 30_000;
/** N17-obs470: the cadence while the backend is down (a deploy restart is ~11 s), so the
 *  shell's 「正在更新……」 clears within seconds of the backend answering again. */
export const HEALTH_DOWN_PROBE_MS = 3_000;
const HEALTH_TIMEOUT_MS = 8_000;

/**
 * Probes `/api/health` on mount, every `HEALTH_PROBE_MS`, and immediately when
 * the window regains connectivity or focus, so the topbar chip recovers
 * without a reload. Only `status` distinguishes "still loading" from "down".
 */
export function useHealth(intervalMs = HEALTH_PROBE_MS): HealthState {
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [status, setStatus] = useState<HealthStatus>("loading");
  const alive = useRef(true);

  const probe = useCallback(async () => {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), HEALTH_TIMEOUT_MS);
    try {
      const res = await fetch("/api/health", { signal: ctl.signal, cache: "no-store" });
      if (!res.ok) throw new Error(`health ${res.status}`);
      const data = (await res.json()) as HealthInfo;
      if (!alive.current) return;
      setHealth(data);
      setStatus("ok");
    } catch {
      // No answer, a 5xx, or a non-JSON body (vite proxy with no upstream):
      // the backend is unreachable from this console's point of view.
      if (alive.current) setStatus("down");
    } finally {
      clearTimeout(timer);
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void probe();
    const timer = setInterval(() => void probe(), intervalMs);
    const wake = () => void probe();
    window.addEventListener("online", wake);
    window.addEventListener("focus", wake);
    return () => {
      alive.current = false;
      clearInterval(timer);
      window.removeEventListener("online", wake);
      window.removeEventListener("focus", wake);
    };
  }, [probe, intervalMs]);

  // N17-obs470: probe faster while down; the steady interval above keeps running
  useEffect(() => {
    if (status !== "down") return;
    const fast = setInterval(() => void probe(), HEALTH_DOWN_PROBE_MS);
    return () => clearInterval(fast);
  }, [status, probe]);

  return { health, status, refresh: () => void probe() };
}
