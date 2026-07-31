import type { ChipTone } from "./Chip";
import { Chip } from "./Chip";

const METHOD_CHIP: Record<string, { tone: ChipTone; icon: string; label: string }> = {
  harness: { tone: "amber", icon: "◇", label: "HARNESS" },
  container: { tone: "blue", icon: "▣", label: "CLAUDE SDK" },
  zip_runtime: { tone: "aqua", icon: "⬡", label: "STRANDS" },
  studio: { tone: "aqua", icon: "⬡", label: "STUDIO" },
  discovered_runtime: { tone: "muted", icon: "◎", label: "DISCOVERED RT" },
};

export function MethodChip({ method }: { method: string }) {
  const display = METHOD_CHIP[method] ?? METHOD_CHIP.harness;
  return (
    <Chip tone={display.tone} icon={display.icon}>
      {display.label}
    </Chip>
  );
}
