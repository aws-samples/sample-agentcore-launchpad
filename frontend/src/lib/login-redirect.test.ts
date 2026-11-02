import { describe, expect, it } from "vitest";

import { loginRedirectTarget } from "./login-redirect";

describe("loginRedirectTarget", () => {
  it("falls back to the index route without a target", () => {
    expect(loginRedirectTarget("")).toBe("/");
    expect(loginRedirectTarget("?foo=bar")).toBe("/");
  });

  it("keeps a same-origin console path, query and hash included", () => {
    expect(loginRedirectTarget("?next=/v2/agents")).toBe("/v2/agents");
    expect(loginRedirectTarget(`?next=${encodeURIComponent("/v2/agents?view=detail&id=a1#x")}`)).toBe(
      "/v2/agents?view=detail&id=a1#x",
    );
    expect(loginRedirectTarget("?redirect=/v3/chat")).toBe("/v3/chat");
  });

  it("refuses off-origin and protocol-relative targets", () => {
    expect(loginRedirectTarget("?next=https://evil.example/")).toBe("/");
    expect(loginRedirectTarget("?next=//evil.example/x")).toBe("/");
    expect(loginRedirectTarget(`?next=${encodeURIComponent("/\\evil.example")}`)).toBe("/");
    expect(loginRedirectTarget("?next=javascript:alert(1)")).toBe("/");
    expect(loginRedirectTarget(`?next=${encodeURIComponent("/v2\n/evil")}`)).toBe("/");
    // surrounding whitespace is trimmed, not a reason to refuse
    expect(loginRedirectTarget(`?next=${encodeURIComponent(" /v2 ")}`)).toBe("/v2");
  });

  it("never loops back onto an auth path", () => {
    expect(loginRedirectTarget("?next=/login")).toBe("/");
    expect(loginRedirectTarget("?next=/login/?next=/v2")).toBe("/");
    expect(loginRedirectTarget("?next=/register")).toBe("/");
  });
});
