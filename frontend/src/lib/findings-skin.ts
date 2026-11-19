import type { ComponentType, ReactNode } from "react";

/**
 * The few console primitives the failure-findings panels need. Each console (V2, V3)
 * supplies its own so the panels are drawn with that console's cards, selects and
 * alerts instead of one hand-styled look (`v2/findingsSkin.tsx`, `v3/findingsSkin.tsx`).
 */
export type NoteTone = "info" | "warn" | "error";

export interface FindingsSkin {
  Section: ComponentType<{ title: ReactNode; children: ReactNode; testId?: string; end?: ReactNode }>;
  Tabs: ComponentType<{
    value: string;
    options: { value: string; label: string }[];
    onChange: (value: string) => void;
    ariaLabel: string;
  }>;
  Pick: ComponentType<{
    value: string;
    options: { value: string; label: string }[];
    placeholder: string;
    onChange: (value: string) => void;
    ariaLabel: string;
    testId?: string;
  }>;
  Note: ComponentType<{ tone: NoteTone; children: ReactNode; action?: ReactNode }>;
  Busy: ComponentType;
  Button: ComponentType<{ children: ReactNode; onClick: () => void; testId?: string }>;
  Tag: ComponentType<{ tone: "red" | "gray"; children: ReactNode }>;
}
