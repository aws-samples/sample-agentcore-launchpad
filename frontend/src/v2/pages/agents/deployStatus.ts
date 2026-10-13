import type { DeploymentInfo, StageInfo } from "../../../lib/api";
import { parseTimestamp } from "../../../lib/timestamps";

/** i18n key (under `v2.agents.progress`) for the plain-language line of the stage
 *  now running. Harness has no build, so one line covers every stage. */
export function progressKey(method: string, stage: StageInfo | undefined): string {
  if (method === "harness") return "harness";
  switch (stage?.name) {
    case "package":
      if (method === "zip_runtime") return "zipPackage";
      if (method === "container" || method === "studio") return "containerBuild";
      return "package";
    case "register":
      return "register";
    case "generate":
      return "prepare";
    default:
      return "start";
  }
}

export function runningStage(deployment: DeploymentInfo): StageInfo | undefined {
  return (
    deployment.stages.find((s) => s.status === "running") ??
    deployment.stages.find((s) => s.status === "pending")
  );
}

export function fmtElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

/** A deploy failed when the deployment says so or any one stage did. */
export function isFailed(deployment: DeploymentInfo): boolean {
  return deployment.status === "failed" || deployment.stages.some((s) => s.status === "failed");
}

/** What the plain-language deploy line says. `now` is injected so it stays pure. */
export type DeployLine =
  | { kind: "none" }
  | { kind: "running"; key: string; elapsedMs: number | null }
  | { kind: "done"; elapsedMs: number | null }
  | { kind: "failed"; stage: string | null };

export function deployLine(
  method: string,
  deployment: DeploymentInfo | null | undefined,
  now: number,
): DeployLine {
  if (!deployment) return { kind: "none" };
  if (isFailed(deployment)) {
    return { kind: "failed", stage: deployment.stages.find((s) => s.status === "failed")?.name ?? null };
  }
  // ledger times are naive UTC; parseTimestamp reads them as such
  const started = parseTimestamp(deployment.started_at)?.getTime() ?? null;
  if (deployment.status === "running") {
    return {
      kind: "running",
      key: progressKey(method, runningStage(deployment)),
      elapsedMs: started != null ? now - started : null,
    };
  }
  const ended = parseTimestamp(deployment.ended_at)?.getTime() ?? now;
  return { kind: "done", elapsedMs: started != null ? ended - started : null };
}
