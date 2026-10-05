import { describe, expect, it } from "vitest";

import { asUserChecked, asUserField } from "./chat";

describe("invoke as me", () => {
  it("omits as_user until the member toggles it, so the backend picks", () => {
    expect(asUserField(true, null)).toBeUndefined();
    expect(asUserField(true, true)).toBe(true);
    expect(asUserField(true, false)).toBe(false);
  });

  it("never sends as_user for a non-JWT agent", () => {
    expect(asUserField(false, null)).toBeUndefined();
    expect(asUserField(false, true)).toBeUndefined();
    expect(asUserField(false, false)).toBeUndefined();
  });

  it("shows auto as checked only when signed in through the pool", () => {
    expect(asUserChecked(null, true)).toBe(true);
    expect(asUserChecked(null, false)).toBe(false);
    expect(asUserChecked(true, false)).toBe(true);
    expect(asUserChecked(false, true)).toBe(false);
  });
});
