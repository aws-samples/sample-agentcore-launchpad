import { renderToStaticMarkup } from "react-dom/server";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import { RegistrationClosedNotice } from "./AuthGate";

const PROD_TEXT = "由于安全原因，已关闭自助注册，请联系后台人员申请账号。";

beforeAll(() => {
  // i18n.ts mirrors the language onto <html lang>; the node test env has no DOM
  vi.stubGlobal("document", { documentElement: {} });
});

afterAll(async () => {
  await i18n.changeLanguage("en");
  vi.unstubAllGlobals();
});

describe("registration-closed notice on the sign-in page", () => {
  it.each(["v2", "v3"] as const)("shows the operator's text as written (%s)", (skin) => {
    const html = renderToStaticMarkup(<RegistrationClosedNotice skin={skin} notice={PROD_TEXT} />);
    expect(html).toContain(PROD_TEXT);
    expect(html).toContain(`class="${skin}-auth-notice"`);
    expect(html).toContain('data-testid="auth-registration-closed"');
  });

  it.each(["v2", "v3"] as const)("falls back to the generic copy (%s)", async (skin) => {
    await i18n.changeLanguage("en");
    expect(renderToStaticMarkup(<RegistrationClosedNotice skin={skin} notice="  " />)).toContain(
      en.auth.registrationClosedNotice,
    );
    await i18n.changeLanguage("zh-CN");
    expect(renderToStaticMarkup(<RegistrationClosedNotice skin={skin} notice="" />)).toContain(
      zh.auth.registrationClosedNotice,
    );
  });

  it("has copy in both locales", () => {
    expect(en.auth.registrationClosedNotice).toMatch(/contact an administrator/i);
    expect(zh.auth.registrationClosedNotice).toBe("自助注册已关闭，如需账号请联系管理员申请。");
  });
});
