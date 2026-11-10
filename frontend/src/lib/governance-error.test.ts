// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import i18n from "../i18n";
import { ApiError } from "./api";
import { governanceError } from "./governance";

afterEach(async () => { await i18n.changeLanguage("en"); });

describe("Gateway schema diagnostics", () => {
  it.each(["en", "zh-CN"])("preserves safe upstream diagnostics in %s", async (locale) => {
    await i18n.changeLanguage(locale);
    const message = i18n.t("apiErrors.policy.engine_install_failed");
    const result = governanceError(new ApiError("policy.engine_install_failed", message, {
      source: "tools_list", upstream_status: 500, attempts: 3, body: "secret", url: "secret",
    }));
    expect(result).toContain(message);
    expect(result).toContain("Gateway tools/list");
    expect(result).toContain("HTTP 500");
    expect(result).toContain("3");
    expect(result).not.toContain("secret");
  });

  it("does not display untrusted diagnostic fields", () => {
    expect(governanceError(new ApiError("policy.engine_install_failed", "safe", {
      source: "tools_list", upstream_status: "secret", attempts: 3,
    }))).toBe("policy.engine_install_failed: safe");
  });
});
