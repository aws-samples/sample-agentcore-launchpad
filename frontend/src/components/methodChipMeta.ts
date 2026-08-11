import type { ChipTone } from "./Chip";

export const METHOD_CHIP: Record<string, { tone: ChipTone; icon: string; label: string }> = {
  harness: { tone: "amber", icon: "◇", label: "HARNESS" },
  // the "Other Agent SDK" entrance; the SDK itself is spec.agent_sdk
  container: { tone: "blue", icon: "▣", label: "AGENT SDK" },
  zip_runtime: { tone: "aqua", icon: "⬡", label: "STRANDS" },
  studio: { tone: "aqua", icon: "⬡", label: "STUDIO" },
  discovered_runtime: { tone: "muted", icon: "◎", label: "DISCOVERED RT" },
};

export const methodLabel = (method: string) =>
  (METHOD_CHIP[method] ?? METHOD_CHIP.harness).label;
