import { afterEach, describe, expect, it, vi } from "vitest";

const sourceLink = vi.fn();
vi.mock("./api", () => ({
  v2KnowledgeApi: { sourceLink: (uri: string) => sourceLink(uri) },
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { isS3ObjectUrl, openKbSource } from "./kb-source";

const B = "launchpad-artifacts-111122223333-us-west-2";

describe("isS3ObjectUrl", () => {
  it.each([
    `https://${B}.s3.us-west-2.amazonaws.com/kb/KB1/04-qdocs-roles.md`,
    `https://${B}.s3.amazonaws.com/kb/KB1/a.md`,
    `https://${B}.s3-us-west-2.amazonaws.com/kb/KB1/a.md`,
    `https://s3.us-west-2.amazonaws.com/${B}/kb/KB1/a.md`,
    `s3://${B}/kb/KB1/a.md`,
  ])("recognises %s", (href) => expect(isS3ObjectUrl(href)).toBe(true));

  it.each([
    undefined,
    "",
    "https://example.com/doc.md",
    `http://${B}.s3.us-west-2.amazonaws.com/kb/a.md`,
    `https://${B}.s3.us-west-2.amazonaws.com/`,
    "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/x",
    "s3://bucket-only",
    "not a url",
  ])("leaves %s alone", (href) => expect(isS3ObjectUrl(href)).toBe(false));
});

describe("openKbSource", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    sourceLink.mockReset();
  });

  it("opens the tab inside the click, then points it at the presigned URL", async () => {
    const tab = { opener: {}, location: { href: "" }, close: vi.fn() };
    const open = vi.fn(() => tab);
    vi.stubGlobal("window", { open });
    sourceLink.mockResolvedValue({ url: "https://signed.example/a.md" });
    const onError = vi.fn();
    const pending = openKbSource(`s3://${B}/kb/KB1/a.md`, onError);
    expect(open).toHaveBeenCalledWith("", "_blank"); // synchronously, before the fetch
    await pending;
    expect(sourceLink).toHaveBeenCalledWith(`s3://${B}/kb/KB1/a.md`);
    expect(tab.opener).toBeNull();
    expect(tab.location.href).toBe("https://signed.example/a.md");
    expect(onError).not.toHaveBeenCalled();
  });

  it("closes the tab and reports a source that cannot be opened", async () => {
    const tab = { opener: {}, location: { href: "" }, close: vi.fn() };
    vi.stubGlobal("window", { open: vi.fn(() => tab) });
    sourceLink.mockRejectedValue(new Error("这篇文档已从知识库中移除。"));
    const onError = vi.fn();
    await openKbSource(`s3://${B}/kb/KB1/gone.md`, onError);
    expect(tab.close).toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith("这篇文档已从知识库中移除。");
  });
});
