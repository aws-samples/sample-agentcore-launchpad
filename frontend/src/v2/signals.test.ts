import { describe, expect, it } from "vitest";

import { SIGNAL_TONE, type Signal, worstSignal } from "./signals";

describe("signals", () => {
  it("gives every signal its own tag tone", () => {
    const tones = Object.values(SIGNAL_TONE);
    expect(Object.keys(SIGNAL_TONE).sort()).toEqual(["act", "info", "off", "ok", "wait"]);
    expect(new Set(tones).size).toBe(tones.length);
  });

  it("ranks act > wait > info > ok > off", () => {
    const order: Signal[] = ["off", "ok", "info", "wait", "act"];
    order.forEach((s, i) => expect(worstSignal(order.slice(0, i + 1))).toBe(s));
    expect(worstSignal(["ok", "act", "wait"])).toBe("act");
  });

  it("has no worst signal for an empty list", () => {
    expect(worstSignal([])).toBeUndefined();
  });
});
