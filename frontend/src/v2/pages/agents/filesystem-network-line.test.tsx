import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { emptyAgentForm } from "../../../lib/agent-spec";

vi.mock("../../../auth/auth-context", () => ({ useAuth: () => ({ isAdmin: false }) }));

const { FilesystemCard } = await import("./MethodSections");

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("FilesystemCard network line", () => {
  it("does not reuse the network card's profile-picker test id", () => {
    // the network card's <Select> owns v2-agent-network-profile; a second element with
    // the same id made selectors (and the workshop capture's highlight) match both
    const form = { ...emptyAgentForm("zip_runtime"), networkProfileId: "np-1", networkVpc: true };
    const html = renderToStaticMarkup(
      <FilesystemCard form={form} set={() => undefined} touched={false} network={false} />,
    );
    expect(html).toContain('data-testid="v2-agent-fs-network-profile"');
    expect(html).not.toContain('data-testid="v2-agent-network-profile"');
  });
});

describe("FilesystemCard session storage and output folder toggles", () => {
  const render = (form: ReturnType<typeof emptyAgentForm>) =>
    renderToStaticMarkup(<FilesystemCard form={form} set={() => undefined} touched={true} network={false} />);
  const checkbox = (html: string, id: string) => html.match(new RegExp(`<input[^>]*data-testid="${id}"[^>]*>`))?.[0] ?? "";

  it("has two independent checkboxes: session storage on, the folder off by default", () => {
    const html = render(emptyAgentForm("zip_runtime"));
    expect(checkbox(html, "v2-agent-fs-session")).toContain("checked");
    expect(checkbox(html, "v2-agent-fs-outputs")).not.toContain("checked");
    expect(html).toContain('data-testid="v2-agent-fs-session-mount"');
    expect(html).not.toContain('data-testid="v2-agent-fs-outputs-mount"');
    expect(html).toContain("survives stop/resume");
    expect(html).toContain("whole files only");
  });

  it("shows both mount paths when both are on, with no conflict message", () => {
    const html = render({ ...emptyAgentForm("zip_runtime"), outputsFs: true });
    expect(checkbox(html, "v2-agent-fs-session")).toContain("checked");
    expect(checkbox(html, "v2-agent-fs-outputs")).toContain("checked");
    expect(html).toContain('data-testid="v2-agent-fs-session-mount"');
    expect(html).toContain('data-testid="v2-agent-fs-outputs-mount"');
    expect(html).not.toContain("v2-agent-fs-both");
  });

  it("leaves the output folder out for the Harness", () => {
    const html = render(emptyAgentForm("harness"));
    expect(html).toContain('data-testid="v2-agent-fs-session"');
    expect(html).not.toContain('data-testid="v2-agent-fs-outputs"');
  });

  it("flags the folder on a V1 Runtime", () => {
    const form = emptyAgentForm("zip_runtime");
    const v1 = render({ ...form, outputsFs: true, runtimeConfig: { ...form.runtimeConfig!, platform_version: "V1" } });
    expect(v1).toContain('data-testid="v2-agent-fs-outputs-v2"');
    expect(render({ ...form, outputsFs: true })).not.toContain("v2-agent-fs-outputs-v2");
  });
});
