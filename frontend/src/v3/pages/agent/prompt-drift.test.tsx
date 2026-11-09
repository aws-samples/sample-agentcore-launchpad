import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import type { AgentPromptDrift } from "../../../lib/api";
import type { Loaded } from "../../hooks";
import { PromptDriftView } from "./PromptDriftCard";

const loaded = (data: AgentPromptDrift): Loaded<AgentPromptDrift> =>
  ({ data, error: null, failure: null, loading: false, reload: () => {} });

const base: AgentPromptDrift = {
  agent_id: "a1", kind: "harness", status: "drifted", ledger_version: "2",
  ledger_prompt: "Be brief.", aws_prompt: "Be brief. Answer in French.", aws_version: "3",
  endpoint_mode: "default", other_differences: [],
};

const render = (drift: AgentPromptDrift, canAdopt = true) =>
  renderToStaticMarkup(<PromptDriftView drift={loaded(drift)} canAdopt={canAdopt} blockedTitle="no" onAdopted={() => {}} />);

describe("prompt drift", () => {
  beforeAll(async () => {
    vi.stubGlobal("document", { documentElement: {} });
    await i18n.changeLanguage("en");
  });

  it("shows both prompts and offers the adopt", () => {
    const html = render(base);
    expect(html).toContain("changed outside Launchpad");
    expect(html).toContain("Be brief. Answer in French.");
    expect(html).toContain("AWS, version 3");
    expect(html).toContain("Adopt the AWS prompt");
    expect(html).not.toContain("production is pinned");
  });

  it("warns about other changes and a gated agent's live endpoint", () => {
    const html = render({ ...base, endpoint_mode: "live",
      other_differences: [{ member: "max_iterations", ledger: 100, aws: 7 }] });
    expect(html).toContain("max_iterations (100 → 7)");
    expect(html).toContain("production is pinned to the live endpoint");
  });

  it("reports in sync and an unreadable runtime prompt", () => {
    expect(render({ ...base, status: "in_sync" })).toContain("In sync");
    const runtime = render({ ...base, kind: "runtime", status: "unsupported", reason: "runtime" });
    expect(runtime).toContain("compiled into its code");
    expect(runtime).not.toContain("Adopt the AWS prompt");
  });
});
