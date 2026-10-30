// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import en from "../locales/en/common.json";
import { isNewVersion } from "./buildSha";
import { NewVersionBanner } from "./NewVersionBanner";

/**
 * N17-obs466 (user request 2026-10-26): an open tab learns that the console was redeployed —
 * its bundle's build sha differs from the sha the served `dist/` was built from
 * (`/api/health` `build.dist_sha`) — and offers a reload. Never reloads by itself: an
 * unsaved editor would be lost.
 */

const A = "a096762d7f3e4c21b9d0e5f6a7b8c9d0e1f2a3b4";
const B = "b33c5c43aa11bb22cc33dd44ee55ff6600112233";

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en", fallbackLng: "en", resources: { en: { common: en } }, defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.restoreAllMocks();
});

describe("isNewVersion", () => {
  it("is true only when both shas are known and name different commits", () => {
    expect(isNewVersion(A, B)).toBe(true);
    expect(isNewVersion(A, A)).toBe(false);
    expect(isNewVersion(A, A.slice(0, 8))).toBe(false); // a short stamp names the same commit
    expect(isNewVersion(A.toUpperCase(), A)).toBe(false);
    expect(isNewVersion(null, B)).toBe(false); // a dev server / unstamped bundle
    expect(isNewVersion(A, null)).toBe(false); // an older backend / unstamped host
    expect(isNewVersion(A, undefined)).toBe(false);
    expect(isNewVersion(A, "not-a-sha")).toBe(false);
  });
});

describe("NewVersionBanner", () => {
  it("renders nothing while this tab runs the served build", () => {
    render(<NewVersionBanner bundleSha={A} servedSha={A} />);
    expect(screen.queryByTestId("v2-new-version")).toBeNull();
  });

  it("offers a reload with the new short sha and never reloads by itself", () => {
    const reload = vi.fn();
    render(<NewVersionBanner bundleSha={A} servedSha={B} reload={reload} />);
    const banner = screen.getByTestId("v2-new-version");
    expect(banner.getAttribute("role")).toBe("status");
    expect(banner.textContent).toContain("b33c5c43");
    expect(reload).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("v2-new-version-reload"));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("「Later」 hides it for that version only; a newer deploy asks again", () => {
    const { rerender } = render(<NewVersionBanner bundleSha={A} servedSha={B} reload={vi.fn()} />);
    fireEvent.click(screen.getByTestId("v2-new-version-later"));
    expect(screen.queryByTestId("v2-new-version")).toBeNull();
    rerender(<NewVersionBanner bundleSha={A} servedSha={B} reload={vi.fn()} />);
    expect(screen.queryByTestId("v2-new-version")).toBeNull();
    const C = "c".repeat(40);
    rerender(<NewVersionBanner bundleSha={A} servedSha={C} reload={vi.fn()} />);
    expect(screen.getByTestId("v2-new-version")).toBeTruthy();
  });

  it("the dismissal survives a remount in the same tab (sessionStorage)", () => {
    render(<NewVersionBanner bundleSha={A} servedSha={B} reload={vi.fn()} />);
    fireEvent.click(screen.getByTestId("v2-new-version-later"));
    cleanup();
    render(<NewVersionBanner bundleSha={A} servedSha={B} reload={vi.fn()} />);
    expect(screen.queryByTestId("v2-new-version")).toBeNull();
  });

  it("goes away again when the served build re-converges (mid-update window)", () => {
    const { rerender } = render(<NewVersionBanner bundleSha={A} servedSha={B} reload={vi.fn()} />);
    expect(screen.getByTestId("v2-new-version")).toBeTruthy();
    rerender(<NewVersionBanner bundleSha={A} servedSha={A} reload={vi.fn()} />);
    expect(screen.queryByTestId("v2-new-version")).toBeNull();
  });
});
