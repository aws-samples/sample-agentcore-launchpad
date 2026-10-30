import type { TFunction } from "i18next";

// Why an agent is NOT eligible for a Runtime canary, as the console renders it in the
// disabled dropdown rows (CanaryStart and the classic canary page). The backend owns the
// verdict (`canary_capability` in optimization/capability.py) and sends BOTH a stable
// `reason_code` and a human `reason`; the console translates the code and, when a code has
// no string in the active locale (N12-obs35: the dropdown once showed a raw `….reason.<code>`
// key), falls back to the server text instead of the key.
// A backend test keeps every emitted code covered in en + zh-CN; this is the belt.
// (The configuration experiment's `expPage` twin retired with it, N13-obs2.)

export type CapabilityPage = "canaryPage";

export interface CapabilityVerdict {
  eligible?: boolean;
  reason?: string | null;
  reason_code?: string | null;
}

// i18next returns `defaultValue` for a missing key; a sentinel (never a real string)
// tells "missing" apart from an empty translation regardless of `returnEmptyString`.
const MISSING = "\u0000missing-translation";

export function capabilityReasonText(
  t: TFunction,
  page: CapabilityPage,
  cap: CapabilityVerdict | null | undefined,
): string {
  const code = cap?.reason_code;
  if (code) {
    const text = t(`${page}.reason.${code}`, { defaultValue: MISSING });
    if (text !== MISSING) return text;
  }
  const server = (cap?.reason ?? "").trim();
  if (server) return server;
  return t(`${page}.reason.unknown`);
}
