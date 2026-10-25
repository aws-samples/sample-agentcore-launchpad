import type { TFunction } from "i18next";
import { describe, expect, it } from "vitest";

import type { MemoryActor } from "../../../lib/api";
import { actorText } from "./common";

const t = ((key: string, opts?: { name?: string }) =>
  key === "v2.memory.deletedAgentNamed" ? `${opts?.name} (deleted)` : key) as unknown as TFunction;

const actor = (over: Partial<MemoryActor>): MemoryActor => ({
  actor_id: "a1__lab01",
  agent_id: "a1",
  agent_name: "lab01-itdesk",
  agent_deleted: false,
  human_actor: "lab01",
  scoped: true,
  ...over,
});

describe("actorText", () => {
  it("tells a deleted agent's partition from its re-created namesake", () => {
    expect(actorText(t, actor({}))).toBe("lab01-itdesk · lab01");
    expect(actorText(t, actor({ agent_deleted: true }))).toBe("lab01-itdesk (deleted) · lab01");
  });

  it("falls back to the generic label when the agent row is gone", () => {
    expect(actorText(t, actor({ agent_name: null, agent_deleted: true }))).toBe(
      "memoryPage.short.deletedAgent · lab01",
    );
  });

  it("shows an unscoped actor as its bare id", () => {
    expect(actorText(t, actor({ scoped: false, agent_id: null, agent_name: null, human_actor: "default" }))).toBe(
      "default",
    );
  });
});
