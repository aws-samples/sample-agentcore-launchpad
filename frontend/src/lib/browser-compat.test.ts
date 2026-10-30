// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import i18next from "i18next";
import en from "../locales/en/common.json";
import { browserUuid, copyBrowserText } from "./browser-compat";

beforeEach(async () => {
  await i18next.init({ lng: "en", resources: { en: { translation: en } } });
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

describe("HTTP-compatible UUIDs", () => {
  it("uses the native UUID when available", () => {
    const native = vi.fn(() => "native-uuid");
    vi.stubGlobal("crypto", { randomUUID: native });
    expect(browserUuid()).toBe("native-uuid");
    expect(native).toHaveBeenCalledOnce();
  });
  it("uses cryptographic bytes and preserves RFC v4 version and variant on HTTP", () => {
    const random = vi.fn((bytes: Uint8Array) => bytes.fill(255));
    vi.stubGlobal("crypto", { getRandomValues: random });
    expect(browserUuid()).toBe("ffffffff-ffff-4fff-bfff-ffffffffffff");
    expect(random).toHaveBeenCalledOnce();
    expect(random.mock.calls[0][0]).toHaveLength(16);
  });
});

describe("copying without secure-context clipboard APIs", () => {
  function clipboard(writeText?: (text: string) => Promise<void>) {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: writeText ? { writeText } : undefined });
  }
  function command(result: boolean) {
    Object.defineProperty(document, "execCommand", { configurable: true, value: vi.fn(() => result) });
  }
  it("reports successful modern copying", async () => {
    const write = vi.fn().mockResolvedValue(undefined);
    clipboard(write);
    expect(await copyBrowserText("policy text")).toBe(true);
    expect(write).toHaveBeenCalledWith("policy text");
    expect(document.querySelector("textarea")).toBeNull();
  });
  it("falls back after modern API permission denial and restores focus", async () => {
    clipboard(vi.fn().mockRejectedValue(new Error("denied")));
    command(true);
    const button = document.createElement("button");
    document.body.append(button); button.focus();
    expect(await copyBrowserText("policy text")).toBe(true);
    expect(document.execCommand).toHaveBeenCalledWith("copy");
    expect(document.activeElement).toBe(button);
    expect(document.querySelector("textarea")).toBeNull();
  });
  it("copies on HTTP with no navigator.clipboard", async () => {
    clipboard(); command(true);
    expect(await copyBrowserText("password")).toBe(true);
    expect(document.querySelector("textarea")).toBeNull();
  });
  it("offers selectable text and never reports copied when both APIs are unavailable", async () => {
    clipboard(); command(false);
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: function (this: HTMLDialogElement) { this.open = true; } });
    Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: function (this: HTMLDialogElement) { this.dispatchEvent(new Event("close")); } });
    const previous = document.createElement("button"); document.body.append(previous); previous.focus();
    expect(await copyBrowserText("secret\nsecond line")).toBe(false);
    const dialog = document.querySelector("dialog")!;
    const field = dialog.querySelector("textarea")!;
    expect(dialog.getAttribute("aria-label")).toBe("Copy text");
    expect(field.value).toBe("secret\nsecond line");
    expect(field.readOnly).toBe(true);
    expect(field.selectionEnd).toBe(field.value.length);
    const underlyingEscape = vi.fn();
    window.addEventListener("keydown", underlyingEscape);
    field.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(underlyingEscape).not.toHaveBeenCalled();
    window.removeEventListener("keydown", underlyingEscape);
    expect(field.value).toBe("");
    expect(document.querySelector("dialog")).toBeNull();
    expect(document.activeElement).toBe(previous);
  });
});
