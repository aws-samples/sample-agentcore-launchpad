// Runtime canaries (champion vs candidate version behind a dedicated gateway,
// ramped 90/10 → 50/50 → 1/99 on real production traffic) — shared rules.
import type { RuntimeCanaryInfo } from "../../../lib/api";
import type { TagTone } from "../../ui";

export const RAMP_STAGES = [
  { control: 90, treatment: 10 },
  { control: 50, treatment: 50 },
  { control: 1, treatment: 99 },
] as const;

export const CANARY_TONE: Record<RuntimeCanaryInfo["status"], TagTone> = {
  running: "blue",
  completed: "green",
  rolled_back: "red",
  cleaned: "gray",
};

export function verdictTone(verdict: string): TagTone {
  if (verdict === "treatment-wins") return "green";
  if (verdict === "control-wins") return "red";
  if (verdict.includes("insufficient") || verdict === "tie") return "orange";
  return "gray";
}

type Setup = RuntimeCanaryInfo["artifacts"]["setup"];

/** `v<current> → v<candidate>`, "…" for what the artifact has not reached yet. */
export function versionsLabel(setup: Setup): string {
  if (!setup) return "—";
  const v = (value?: string) => (value ? `v${value}` : "…");
  return `${v(setup.v_current)} → ${v(setup.v_candidate)}`;
}

export function weightsLabel(setup: Setup): string {
  const w = setup?.weights;
  return w ? `${w.C ?? "—"}/${w.T1 ?? "—"}` : "—";
}
