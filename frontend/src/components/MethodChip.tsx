import { Chip } from "./Chip";
import { METHOD_CHIP } from "./methodChipMeta";

export function MethodChip({ method }: { method: string }) {
  const display = METHOD_CHIP[method] ?? METHOD_CHIP.harness;
  return (
    <Chip tone={display.tone} icon={display.icon}>
      {display.label}
    </Chip>
  );
}
