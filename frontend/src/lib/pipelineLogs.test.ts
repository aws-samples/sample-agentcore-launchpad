import { describe, expect, it } from "vitest";

import type { V2PipelineSource } from "./api";
import { cleanSource, defaultLogFormat, logSourceProblem, pathOptions, switchPreset } from "./pipelineLogs";

const base: V2PipelineSource = { type: "logs", agent: null, range: "24h", status: "all", max_sessions: 20, log_groups: ["/my/app"], keyword: "  ", format: defaultLogFormat() };

describe("cleanSource", () => {
  it("normalises a logs source: empty fields → null, roles trimmed, keyword dropped", () => {
    const out = cleanSource({ ...base, format: { ...defaultLogFormat(), preset: "message", session_field: " conv ", user_roles: ["user", " human", ""] } });
    expect(out.keyword).toBeNull();
    expect(out.format).toMatchObject({ preset: "message", session_field: "conv", role_field: null, user_roles: ["user", "human"] });
  });

  it("sends only the preset's own fields", () => {
    const fmt = { ...defaultLogFormat(), preset: "exchange" as const, input_field: "q", role_field: "role", text_field: "content" };
    expect(cleanSource({ ...base, format: fmt }).format).toMatchObject({ input_field: "q", role_field: null, text_field: null });
  });

  it("falls back to the default roles when every entry was cleared", () => {
    expect(cleanSource({ ...base, format: { ...defaultLogFormat(), assistant_roles: [" "] } }).format?.assistant_roles).toEqual(["assistant", "ai", "bot"]);
  });

  it("strips the logs-only fields from a traces source", () => {
    expect(cleanSource({ ...base, type: "traces" })).toEqual({ type: "traces", agent: null, range: "24h", status: "all", max_sessions: 20 });
  });
});

describe("logSourceProblem", () => {
  it("needs log groups and each preset's fields", () => {
    expect(logSourceProblem({ ...base, log_groups: [] })).toBe("noGroups");
    expect(logSourceProblem(base)).toBeNull(); // genai needs nothing else
    expect(logSourceProblem({ ...base, format: { ...defaultLogFormat(), preset: "message", session_field: "c", role_field: "r" } })).toBe("needText");
    expect(logSourceProblem({ ...base, format: { ...defaultLogFormat(), preset: "exchange" } })).toBe("needInput");
    expect(logSourceProblem({ ...base, format: { ...defaultLogFormat(), preset: "exchange", input_field: "q; drop" } })).toBe("badPath");
    expect(logSourceProblem({ ...base, format: { ...defaultLogFormat(), preset: "exchange", input_field: "payload.0.q", session_field: "@logStream" } })).toBeNull();
  });
});

it("offers @logStream first for session keys only", () => {
  expect(pathOptions(["a", "@logStream"], true)).toEqual(["@logStream", "a"]);
  expect(pathOptions(["a"])).toEqual(["a"]);
});

it("clears the session field when genai is entered or left, keeps it between the others", () => {
  const message = { ...defaultLogFormat(), preset: "message" as const, session_field: "conv" };
  expect(switchPreset(message, "exchange").session_field).toBe("conv");
  expect(switchPreset(message, "genai").session_field).toBe("");
  expect(switchPreset({ ...defaultLogFormat(), session_field: "x" }, "message").session_field).toBe("");
});
