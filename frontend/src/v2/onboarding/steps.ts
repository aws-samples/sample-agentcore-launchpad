// The V2 launch checklist (ported from console-v3's launch sequence): five steps
// from an empty workspace to an agent whose live traffic is being scored. Each
// step is read from what actually exists, never from a click on the checklist.
// Pure, so it is unit-tested.
import type { AgentInfo, Workspace } from "../../lib/api";
import type { V2Task } from "../tasks";

export type LaunchStepKey = "workspace" | "agent" | "chat" | "evaluate" | "online";

export interface LaunchStep {
  key: LaunchStepKey;
  done: boolean;
  /** where the step's action lives; null when the viewer cannot act on it */
  href: string | null;
  /** i18n key of the line under the step's title */
  hintKey: string;
}

/** How many live agents the checklist asks about chat sessions (one request each). */
export const CHAT_PROBE_LIMIT = 3;

/** The agents whose chat sessions count for step 3: the first few live ones. */
export function chatProbeIds(agents: Pick<AgentInfo, "id" | "status">[]): string[] {
  return agents
    .filter((a) => a.status === "active")
    .slice(0, CHAT_PROBE_LIMIT)
    .map((a) => a.id);
}

export function launchSteps(input: {
  workspace: Pick<Workspace, "id" | "bootstrap_status"> | null;
  agents: Pick<AgentInfo, "id" | "status">[];
  /** ids of probed agents that have at least one chat session */
  chattedAgentIds: string[];
  /** Home's evaluation tasks; null while they load */
  tasks: Pick<V2Task, "kind">[] | null;
  isAdmin: boolean;
}): LaunchStep[] {
  const live = input.agents.filter((a) => a.status === "active");
  const first = live[0]?.id;
  const agentParam = first ? `&agent=${encodeURIComponent(first)}` : "";
  const liveIds = new Set(live.map((a) => a.id));
  const tasks = input.tasks ?? [];
  const ws = input.workspace;
  return [
    {
      key: "workspace",
      done: ws?.bootstrap_status === "ready",
      // the workspaces page is admin-only: a member is told who bootstraps it instead
      href: input.isAdmin && ws ? `/v2/workspaces?view=detail&id=${encodeURIComponent(ws.id)}` : null,
      hintKey: input.isAdmin ? "v2.onboard.launch.workspaceHint" : "v2.onboard.launch.workspaceHintMember",
    },
    {
      key: "agent",
      done: live.length > 0,
      href: "/v2/agents?view=new",
      hintKey: "v2.onboard.launch.agentHint",
    },
    {
      key: "chat",
      done: input.chattedAgentIds.some((id) => liveIds.has(id)),
      href: first ? `/v2/chat?agent=${encodeURIComponent(first)}` : "/v2/chat",
      hintKey: "v2.onboard.launch.chatHint",
    },
    {
      key: "evaluate",
      done: tasks.some((task) => task.kind === "run"),
      href: `/v2/eval/tasks?view=new${agentParam}`,
      hintKey: "v2.onboard.launch.evaluateHint",
    },
    {
      key: "online",
      done: tasks.some((task) => task.kind === "online"),
      href: "/v2/eval/tasks?view=new&strategy=continuous",
      hintKey: "v2.onboard.launch.onlineHint",
    },
  ];
}

/** The first step still to do, or null once every step is done. */
export function nextStep(steps: LaunchStep[]): LaunchStep | null {
  return steps.find((step) => !step.done) ?? null;
}
