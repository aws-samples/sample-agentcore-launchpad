// One status vocabulary for V2 (ported from the V3 console): every page maps its own
// statuses onto a Signal, so a colour means the same thing everywhere.
//   ok   — running as intended        wait — settling, or waiting on someone
//   act  — needs you                  info — moving on its own
//   off  — inactive
import type { TagTone } from "./ui";

export type Signal = "ok" | "wait" | "act" | "info" | "off";

export const SIGNAL_TONE: Record<Signal, TagTone> = {
  ok: "green",
  wait: "orange",
  act: "red",
  info: "blue",
  off: "gray",
};

/** Severity order: the first entry outranks the rest. */
const SEVERITY: Signal[] = ["act", "wait", "info", "ok", "off"];

/** The most severe signal of a list (a panel's edge colour); undefined when empty. */
export function worstSignal(signals: Signal[]): Signal | undefined {
  return SEVERITY.find((s) => signals.includes(s));
}
