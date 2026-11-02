import { describe, expect, it } from "vitest";

import { agentProjectSettingsSource, needsConfirm } from "./agent-project-settings";
import type { AgentProjectSettings } from "./api";

const view = (over: Partial<AgentProjectSettings>): AgentProjectSettings => ({
  allow_default_project: false,
  source: "default",
  console_value: null,
  env: "LAUNCHPAD_AGENT_ALLOW_DEFAULT_PROJECT",
  env_value: null,
  default: false,
  updated_by: null,
  updated_at: null,
  ...over,
});
const t = (key: string, opts?: Record<string, unknown>) => `${key}${opts ? JSON.stringify(opts) : ""}`;

describe("agentProjectSettingsSource", () => {
  it("names who set a console override", () => {
    const text = agentProjectSettingsSource(view({ source: "console", updated_by: "admin" }), t);
    expect(text).toContain("sourceConsole");
    expect(text).toContain("admin");
  });

  it("names the env variable, the yaml file or the built-in default", () => {
    expect(agentProjectSettingsSource(view({ source: "env" }), t)).toContain("LAUNCHPAD_AGENT_ALLOW_DEFAULT_PROJECT");
    expect(agentProjectSettingsSource(view({ source: "config" }), t)).toContain("sourceConfig");
    expect(agentProjectSettingsSource(view({}), t)).toContain("sourceDefault");
  });
});

describe("needsConfirm", () => {
  it("asks only when turning the switch on", () => {
    expect(needsConfirm(false, true)).toBe(true);
    expect(needsConfirm(true, false)).toBe(false);
    expect(needsConfirm(true, true)).toBe(false);
  });
});
