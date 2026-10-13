import { describe, expect, it } from "vitest";

import type { AgentInfo } from "../../lib/api";
import { chatProbeIds, launchSteps, nextStep } from "./steps";

type Agent = Pick<AgentInfo, "id" | "status">;

const base = {
  workspace: { id: "ws-1", bootstrap_status: "ready" as const },
  agents: [{ id: "a1", status: "active" }] as Agent[],
  chattedAgentIds: [] as string[],
  tasks: [] as { kind: "run" | "online" }[],
  isAdmin: true,
};

const step = (steps: ReturnType<typeof launchSteps>, key: string) => steps.find((s) => s.key === key)!;

describe("launchSteps", () => {
  it("lists the five steps in order, ticked from what exists", () => {
    const steps = launchSteps(base);
    expect(steps.map((s) => [s.key, s.done])).toEqual([
      ["workspace", true],
      ["agent", true],
      ["chat", false],
      ["evaluate", false],
      ["online", false],
    ]);
  });

  it("the workspace is done only once bootstrapped", () => {
    for (const status of ["registered", "bootstrapping", "failed"] as const) {
      expect(step(launchSteps({ ...base, workspace: { id: "ws-1", bootstrap_status: status } }), "workspace").done).toBe(false);
    }
    expect(step(launchSteps({ ...base, workspace: null }), "workspace").done).toBe(false);
  });

  it("an admin is linked to the workspace; a member gets no link and an explanation", () => {
    const admin = step(launchSteps(base), "workspace");
    expect(admin.href).toBe("/v2/workspaces?view=detail&id=ws-1");
    expect(admin.hintKey).toBe("v2.onboard.launch.workspaceHint");
    const member = step(launchSteps({ ...base, isAdmin: false }), "workspace");
    expect(member.href).toBeNull();
    expect(member.hintKey).toBe("v2.onboard.launch.workspaceHintMember");
    expect(step(launchSteps({ ...base, workspace: null }), "workspace").href).toBeNull();
  });

  it("an agent counts only once it is live", () => {
    for (const status of ["draft", "deploying", "failed", "deleted"] as const) {
      expect(step(launchSteps({ ...base, agents: [{ id: "a1", status }] }), "agent").done).toBe(false);
    }
    expect(step(launchSteps({ ...base, agents: [] }), "agent").done).toBe(false);
  });

  it("chat is done when a live agent has a session", () => {
    expect(step(launchSteps({ ...base, chattedAgentIds: ["a1"] }), "chat").done).toBe(true);
    // a session on an agent that is no longer live does not count
    expect(step(launchSteps({ ...base, chattedAgentIds: ["gone"] }), "chat").done).toBe(false);
  });

  it("evaluate needs a run, online needs an online config", () => {
    const runOnly = launchSteps({ ...base, tasks: [{ kind: "run" }] });
    expect(step(runOnly, "evaluate").done).toBe(true);
    expect(step(runOnly, "online").done).toBe(false);
    const onlineOnly = launchSteps({ ...base, tasks: [{ kind: "online" }] });
    expect(step(onlineOnly, "evaluate").done).toBe(false);
    expect(step(onlineOnly, "online").done).toBe(true);
    const loading = launchSteps({ ...base, tasks: null });
    expect(step(loading, "evaluate").done).toBe(false);
    expect(step(loading, "online").done).toBe(false);
  });

  it("points chat and evaluate at the first live agent", () => {
    const steps = launchSteps({
      ...base,
      agents: [
        { id: "draft-1", status: "draft" },
        { id: "a/1", status: "active" },
        { id: "a2", status: "active" },
      ],
    });
    expect(step(steps, "agent").href).toBe("/v2/agents?view=new");
    expect(step(steps, "chat").href).toBe("/v2/chat?agent=a%2F1");
    expect(step(steps, "evaluate").href).toBe("/v2/eval/tasks?view=new&agent=a%2F1");
    expect(step(steps, "online").href).toBe("/v2/eval/online?view=new");
    const none = launchSteps({ ...base, agents: [] });
    expect(step(none, "chat").href).toBe("/v2/chat");
    expect(step(none, "evaluate").href).toBe("/v2/eval/tasks?view=new");
  });
});

describe("nextStep", () => {
  it("is the first undone step, even when later ones are done", () => {
    expect(nextStep(launchSteps(base))?.key).toBe("chat");
    const skipped = launchSteps({ ...base, tasks: [{ kind: "run" }, { kind: "online" }] });
    expect(nextStep(skipped)?.key).toBe("chat");
    expect(nextStep(launchSteps({ ...base, workspace: null }))?.key).toBe("workspace");
  });

  it("is null when everything is done", () => {
    const all = launchSteps({ ...base, chattedAgentIds: ["a1"], tasks: [{ kind: "online" }, { kind: "run" }] });
    expect(all.every((s) => s.done)).toBe(true);
    expect(nextStep(all)).toBeNull();
  });
});

describe("chatProbeIds", () => {
  it("takes the first three live agents in list order", () => {
    const agents: Agent[] = [
      { id: "a1", status: "active" },
      { id: "d1", status: "deploying" },
      { id: "a2", status: "active" },
      { id: "a3", status: "active" },
      { id: "a4", status: "active" },
    ];
    expect(chatProbeIds(agents)).toEqual(["a1", "a2", "a3"]);
    expect(chatProbeIds([])).toEqual([]);
  });
});
