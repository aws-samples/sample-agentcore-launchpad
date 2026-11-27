import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { emptyAgentForm, sessionKindFlags } from "../../../lib/agent-spec";

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

describe("FilesystemCard session file storage choice", () => {
  const render = (form: ReturnType<typeof emptyAgentForm>) =>
    renderToStaticMarkup(<FilesystemCard form={form} set={() => undefined} touched={true} network={false} />);
  const card = (html: string, kind: string) => html.match(new RegExp(`<button[^>]*data-testid="v2-agent-fs-kind-${kind}"`))?.[0] ?? "";

  it("offers managed / S3 bucket / none on a Runtime method, one selected", () => {
    const html = render(emptyAgentForm("zip_runtime"));
    for (const kind of ["managed", "s3", "none"]) expect(html).toContain(`v2-agent-fs-kind-${kind}`);
    expect(card(html, "managed")).toContain('aria-pressed="true"');
    expect(card(html, "s3")).toContain('aria-pressed="false"');
    expect(html).toContain('data-testid="v2-agent-fs-session-mount"');
    expect(html).not.toContain('data-testid="v2-agent-fs-outputs-mount"');
    expect(html).not.toContain("v2-agent-fs-session\"");
  });

  it("shows the output folder input for the S3 bucket and none for neither", () => {
    const s3 = render({ ...emptyAgentForm("zip_runtime"), ...sessionKindFlags("s3") });
    expect(card(s3, "s3")).toContain('aria-pressed="true"');
    expect(s3).toContain('data-testid="v2-agent-fs-outputs-mount"');
    expect(s3).not.toContain('data-testid="v2-agent-fs-session-mount"');
    const none = render({ ...emptyAgentForm("zip_runtime"), ...sessionKindFlags("none") });
    expect(card(none, "none")).toContain('aria-pressed="true"');
    expect(none).not.toContain("fs-session-mount");
    expect(none).not.toContain("fs-outputs-mount");
  });

  it("leaves the S3 bucket out for the Harness", () => {
    const html = render(emptyAgentForm("harness"));
    expect(html).toContain("v2-agent-fs-kind-managed");
    expect(html).toContain("v2-agent-fs-kind-none");
    expect(html).not.toContain("v2-agent-fs-kind-s3");
  });

  it("flags both stores, and the S3 bucket on a V1 Runtime", () => {
    const both = render({ ...emptyAgentForm("zip_runtime"), outputsFs: true });
    expect(both).toContain('data-testid="v2-agent-fs-both"');
    expect(card(both, "managed")).toContain('aria-pressed="false"');
    const v1 = render({ ...emptyAgentForm("zip_runtime"), ...sessionKindFlags("s3"), runtimeConfig: { platform_version: "V1" } as never });
    expect(v1).toContain('data-testid="v2-agent-fs-outputs-v2"');
  });
});
