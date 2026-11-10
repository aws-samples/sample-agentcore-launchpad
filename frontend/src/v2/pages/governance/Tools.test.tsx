// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import en from "../../../locales/en/common.json";
import { BrowserDemo } from "./Tools";

/**
 * N17-obs258 (user ruling 2026-10-22, 「隐藏 web bot auth」): the China partition has no Web
 * Bot Auth signer, so the Browser demo hides the 「启用 Web Bot Auth」 toggle and the signed
 * browser selector whenever `GET /api/capabilities.tools.browser_signing` is false — the
 * ordinary managed-browser demo (URL, profile, start) stays.
 */

let signing = false;
vi.mock("../../../lib/capabilities", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/capabilities")>()),
  useBrowserSigningSupported: () => signing,
}));

const runBrowserDemo = vi.fn(async () => {
  throw new Error("stop here");
});
vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      browserDemoOptions: vi.fn(async () => ({
        browsers: [
          { identifier: "signed-1", name: "signed", description: "", status: "READY", web_bot_auth: true },
        ],
        profiles: [],
      })),
      runBrowserDemo: (...args: unknown[]) => runBrowserDemo(...(args as [])),
      stopBrowserDemo: vi.fn(async () => ({ session_id: "", stopped: true, profile_saved: null })),
    },
  };
});

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en",
    fallbackLng: "en",
    resources: { en: { common: en } },
    defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

afterEach(() => {
  cleanup();
  runBrowserDemo.mockClear();
});

describe("V2 Governance Browser demo — Web Bot Auth follows the partition (N17-obs258)", () => {
  it("hides the Web Bot Auth toggle and selector in aws-cn, keeps the ordinary demo", async () => {
    signing = false;
    render(<BrowserDemo />);

    expect(screen.getByTestId("browser-url")).toBeTruthy();
    expect(screen.getByTestId("browser-profile")).toBeTruthy();
    expect(screen.queryByTestId("browser-web-bot-auth")).toBeNull();
    expect(screen.queryByTestId("browser-resource")).toBeNull();
    expect(screen.queryByText(en.v2.governance.demo.webBotAuth)).toBeNull();
    expect(screen.queryByText(en.v2.governance.demo.requestSigning)).toBeNull();

    const start = screen.getByTestId("v2-governance-br-start") as HTMLButtonElement;
    expect(start.disabled).toBe(false);
    fireEvent.click(start);
    await waitFor(() => expect(runBrowserDemo).toHaveBeenCalledTimes(1));
    expect(runBrowserDemo).toHaveBeenCalledWith(
      expect.objectContaining({ web_bot_auth: false, browser_identifier: null }),
    );
  });

  it("offers the toggle and the signed browser selector where the partition signs", async () => {
    signing = true;
    render(<BrowserDemo />);

    const toggle = screen.getByTestId("browser-web-bot-auth") as HTMLInputElement;
    expect(screen.queryByTestId("browser-resource")).toBeNull();
    fireEvent.click(toggle);
    await waitFor(() => expect(screen.getByTestId("browser-resource")).toBeTruthy());
  });
});
