import type { AgentInfo } from "../../../lib/api";

/**
 * Service-health rows (mirrors the classic Overview): "bootstrap" rows come from
 * `make bootstrap` — missing is something to fix; "usage" rows count what the
 * operator has created — missing is the normal state of a fresh account.
 */
export const SERVICES = [
  { id: "runtime", kind: "usage", to: "/v2/agents" },
  { id: "gateway", kind: "bootstrap", to: "/v2/governance" },
  { id: "memory", kind: "bootstrap", to: "/v2/memory" },
  { id: "registry", kind: "bootstrap", to: "/v2/registry" },
  { id: "policy", kind: "usage", to: "/v2/governance" },
  { id: "evaluation", kind: "usage", to: "/v2/eval/tasks" },
  { id: "observability", kind: "bootstrap", to: "/v2/observability" },
] as const;

/** One-line deploy progress for an agent row (same reading as the classic feed). */
export function stageSummary(agent: AgentInfo): string {
  const stages = agent.deployment?.stages ?? [];
  const failed = stages.find((s) => s.status === "failed");
  if (failed) return `${failed.name} ✕`;
  const running = stages.find((s) => s.status === "running");
  if (running) return `${running.name} ◐`;
  const done = stages.filter((s) => s.status === "succeeded" || s.status === "skipped").length;
  if (stages.length > 0 && done === stages.length) return "register ✓";
  return stages.length ? `${done}/${stages.length}` : "—";
}

// Per-viewer "new" marker for announcements: the newest publish time this browser
// has already shown. Browser storage may be unavailable (private mode) — then
// nothing is marked new, which is the safe reading.
const SEEN_KEY = "launchpad.v2.announcements.seenAt";

export function readSeenAt(): string | null {
  try {
    return localStorage.getItem(SEEN_KEY);
  } catch {
    return null;
  }
}

export function writeSeenAt(iso: string): void {
  try {
    localStorage.setItem(SEEN_KEY, iso);
  } catch {
    /* storage blocked — the marker simply stays off */
  }
}
