import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { type AgentForm, emptyAgentForm } from "../../../lib/agent-spec";
import type { Select } from "../../ui";

const { controls } = vi.hoisted(() => ({ controls: new Map<string, Parameters<typeof Select>[0]>() }));
vi.mock("../../ui", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../ui")>();
  return { ...original, Select: (props: Parameters<typeof Select>[0]) => {
    if (props.testId) controls.set(props.testId, props);
    return <original.Select {...props} />;
  } };
});

const { RuntimeConfigCard } = await import("./RuntimeConfigCard");
const { WizardReview } = await import("./WizardReview");

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});
beforeEach(() => controls.clear());

const baseline = { platform_version: "V2" as const, billing_model: "committed_baseline" as const, minimum_memory_mib: 4096 };
const form = (): AgentForm => ({ ...emptyAgentForm("zip_runtime"), name: "runtime-agent", systemPrompt: "Help." });
const render = (draft: AgentForm, preserve = false, touched = false) => {
  const set = vi.fn();
  const html = renderToStaticMarkup(<RuntimeConfigCard form={draft} set={set} preserve={preserve} touched={touched} />);
  return { html, set };
};
const control = (id: string) => {
  const result = controls.get(id);
  expect(result).toBeDefined();
  return result!;
};

describe("Runtime configuration card", () => {
  it("shows V2 and consumption by default, with no paid baseline chosen", () => {
    const { html } = render(form());
    expect(control("v2-agent-platform-version").value).toBe("V2");
    expect(control("v2-agent-billing-model").value).toBe("consumption");
    expect(html).not.toContain('data-testid="v2-agent-runtime-baseline"');
    expect(html).toContain("creation and updates can take several minutes");
    expect(html).toContain("runtime-how-it-works.html#runtime-platform-versions");
  });

  it("leaves legacy settings untouched until the member explicitly selects replacements", () => {
    const { html, set } = render({ ...form(), runtimeConfig: null }, true);
    expect(html).toContain("Keep existing settings");
    expect(control("v2-agent-billing-model").disabled).toBe(true);
    expect(set).not.toHaveBeenCalled();
    control("v2-agent-platform-version").onChange("V2");
    expect(set).toHaveBeenCalledWith({ runtimeConfig: form().runtimeConfig });
  });

  it("requires an explicit size and renders memory/vCPU choices", () => {
    const missing = { ...form(), runtimeConfig: { ...baseline, minimum_memory_mib: null } };
    const { html, set } = render(missing, false, true);
    expect(html).toContain("Choose a supported baseline");
    expect(control("v2-agent-runtime-baseline").options.map((o) => o.label)).toEqual([
      "1 GiB memory · 0.25 vCPU", "2 GiB memory · 0.5 vCPU", "4 GiB memory · 1 vCPU", "8 GiB memory · 2 vCPU",
    ]);
    control("v2-agent-runtime-baseline").onChange("4096");
    expect(set).toHaveBeenCalledWith({ runtimeConfig: baseline });
  });

  it("changes to consumption deliberately and blocks committed choices on V1", () => {
    const { set } = render({ ...form(), runtimeConfig: baseline });
    control("v2-agent-billing-model").onChange("committed_baseline");
    expect(set).not.toHaveBeenCalled();
    control("v2-agent-billing-model").onChange("consumption");
    expect(set).toHaveBeenCalledWith({ runtimeConfig: form().runtimeConfig });
    control("v2-agent-platform-version").onChange("V1");
    const v1 = { platform_version: "V1" as const, billing_model: "consumption" as const, minimum_memory_mib: null };
    expect(set).toHaveBeenCalledWith({ runtimeConfig: v1 });
    render({ ...form(), runtimeConfig: v1 });
    expect(control("v2-agent-billing-model").options.find((o) => o.value === "committed_baseline")?.disabled).toBe(true);
  });

  it("allows returning an edit to its existing configuration", () => {
    const { set } = render({ ...form(), runtimeConfig: baseline }, true);
    control("v2-agent-platform-version").onChange("");
    expect(set).toHaveBeenCalledWith({ runtimeConfig: null });
  });

  it("does not expose unsupported controls for Harness", () => {
    expect(render({ ...form(), method: "harness" }).html).toBe("");
    expect(controls.size).toBe(0);
  });
});

describe("Runtime review", () => {
  const review = (draft: AgentForm) => renderToStaticMarkup(<WizardReview form={draft}
    cat={{ loading: false, gatewayTargets: [], remoteMcp: [], skills: [], kbCatalog: [], memories: [] }}
    ui={{ customModel: false, customSkills: [], byocUpload: null, byocUploading: false }} />);

  it("shows the selected platform, baseline and session-duration charge", () => {
    const html = review({ ...form(), runtimeConfig: baseline });
    expect(html).toContain("Platform version");
    expect(html).toContain("V2");
    expect(html).toContain("Committed baseline (V2 only)");
    expect(html).toContain("4 GiB memory · 1 vCPU");
    expect(html).toContain("including idle time");
  });

  it("does not claim an unconfigured legacy agent uses the new default", () => {
    const html = review({ ...form(), runtimeConfig: null });
    expect(html).toContain("Keep existing settings");
    expect(html).not.toContain("Consumption-based");
  });
});
