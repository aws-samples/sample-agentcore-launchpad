// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AuthContext, useAuth } from "../../../auth/auth-context";
import i18n from "../../../i18n";
import { TelemetryScopeNote } from "./TelemetryScopeNote";

afterEach(cleanup);

function Scope({ admin }: { admin: boolean }) {
  const auth = useAuth();
  return <AuthContext.Provider value={{ ...auth, isAdmin: admin }}>
    <TelemetryScopeNote />
  </AuthContext.Provider>;
}

describe("telemetry scope", () => {
  it.each(["en", "zh-CN"])("distinguishes account and workspace access in %s", async (locale) => {
    await i18n.changeLanguage(locale);
    const first = render(<Scope admin />);
    expect(screen.getByTestId("telemetry-scope-note").textContent)
      .toBe(i18n.t("v2.observability.scopeAdmin"));
    first.unmount();
    render(<Scope admin={false} />);
    expect(screen.getByTestId("telemetry-scope-note").textContent)
      .toBe(i18n.t("v2.observability.scopeMember"));
  });
});
