// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { api, type AuthStatus, type PasswordPolicy } from "../lib/api";
import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import { AuthGate, passwordHintKey } from "./AuthGate";

/**
 * N14-obs3 (r14-c, user decision 2026-10-19): the registration form hints and
 * pre-validates from the `password_policy` the server advertises on
 * `GET /api/auth/status` — never from a hard-coded "12" or rule text. In relaxed mode
 * the hint names only the length.
 */

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en",
    fallbackLng: "en",
    resources: { en: { common: en }, "zh-CN": { common: zh } },
    defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

afterEach(async () => {
  cleanup();
  vi.restoreAllMocks();
  await i18next.changeLanguage("en");
});

const loggedOut = (password_policy: PasswordPolicy): AuthStatus => ({
  auth_required: true,
  authenticated: false,
  registration_enabled: true,
  registration_requires_approval: true,
  password_policy,
  username: null,
  user_id: null,
  role: null,
  email: null,
  account_expires_at: null,
  permissions: [],
});

async function openRegisterForm(policy: PasswordPolicy): Promise<string> {
  vi.spyOn(api, "authStatus").mockResolvedValue(loggedOut(policy));
  render(
    <AuthGate>
      <div>app</div>
    </AuthGate>,
  );
  fireEvent.click(await screen.findByTestId("auth-tab-register"));
  return screen.getByTestId("reg-password-hint").textContent ?? "";
}

describe("registration password hint follows the advertised policy (N14-obs3)", () => {
  it("strict mode names the configured minimum and the three rules", async () => {
    const hint = await openRegisterForm({ mode: "strict", min_length: 12 });
    expect(hint).toBe(
      "At least 12 characters; not your username or email, and not a common password.",
    );
  });

  it("relaxed mode names only the configured minimum — no rule text", async () => {
    const hint = await openRegisterForm({ mode: "relaxed", min_length: 8 });
    expect(hint).toBe("At least 8 characters.");
    expect(hint).not.toMatch(/username|email|common/);
    expect(hint).not.toContain("12");
  });

  it("relaxed mode at the floor, in zh-CN with full-width punctuation", async () => {
    await i18next.changeLanguage("zh-CN");
    const hint = await openRegisterForm({ mode: "relaxed", min_length: 6 });
    expect(hint).toBe("至少 6 个字符。");
  });

  it("pre-validates the length from the policy and never calls the API for a short one", async () => {
    const register = vi.spyOn(api, "register");
    await openRegisterForm({ mode: "relaxed", min_length: 8 });
    fireEvent.change(screen.getByLabelText(en.auth.v2.username), { target: { value: "newbie" } });
    fireEvent.change(screen.getByLabelText(en.auth.v2.companyEmail), {
      target: { value: "newbie@acme-corp.com" },
    });
    fireEvent.change(screen.getByLabelText(en.auth.v2.password), { target: { value: "seven77" } });
    fireEvent.change(screen.getByLabelText(en.auth.v2.confirmPassword), {
      target: { value: "seven77" },
    });
    fireEvent.submit(
      screen.getByLabelText(en.auth.v2.password).closest("form") as HTMLFormElement,
    );
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toBe("Password must be at least 8 characters."),
    );
    expect(register).not.toHaveBeenCalled();
  });

  it("maps the policy mode to the hint key", () => {
    expect(passwordHintKey({ mode: "strict", min_length: 12 })).toBe("auth.passwordHint");
    expect(passwordHintKey({ mode: "relaxed", min_length: 6 })).toBe("auth.passwordHintRelaxed");
  });
});
