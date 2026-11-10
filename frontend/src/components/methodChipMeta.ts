import type { ChipTone } from "./Chip";

export const METHOD_CHIP: Record<string, { tone: ChipTone; icon: string; label: string }> = {
  harness: { tone: "amber", icon: "◇", label: "HARNESS" },
  zip_runtime: { tone: "aqua", icon: "⬡", label: "STRANDS" },
  studio: { tone: "aqua", icon: "⬡", label: "STUDIO" },
  byoc: { tone: "good", icon: "⬆", label: "BYOC" },
  discovered_runtime: { tone: "muted", icon: "◎", label: "DISCOVERED RT" },
};

export const methodLabel = (method: string) =>
  (METHOD_CHIP[method] ?? METHOD_CHIP.harness).label;
