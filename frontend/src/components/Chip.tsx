import type { CSSProperties, ReactNode } from "react";

export type ChipTone = "good" | "warn" | "crit" | "muted" | "amber" | "blue" | "aqua";

interface ChipProps {
  tone?: ChipTone;
  icon?: ReactNode;
  className?: string;
  style?: CSSProperties;
  /** native tooltip — the protected-actions explanation on the SYSTEM chip */
  title?: string;
  children?: ReactNode;
}

export function Chip({ tone, icon, className = "", style, title, children }: ChipProps) {
  return (
    <span
      className={["chip", tone ?? "", className].filter(Boolean).join(" ")}
      style={style}
      title={title}
    >
      {icon != null && <i>{icon}</i>}
      {children}
    </span>
  );
}
