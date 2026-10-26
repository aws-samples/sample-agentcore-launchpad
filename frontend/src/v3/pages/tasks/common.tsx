import type { ReactNode } from "react";
import { createPortal } from "react-dom";

import { fmtScore } from "../../../v2/format";
import { V2ToastProvider } from "../../../v2/ui";
import { Reasoned } from "../../reason";
import type { DisabledReason } from "../../reasons";
import { Chip } from "../../ui";
import { scoreSignal } from "./model";

export function ScoreChip({ value }: { value: number | null }) {
  if (value == null) return <span style={{ color: "var(--v3-text-3)" }}>—</span>;
  return <Chip s={scoreSignal(value)}>{fmtScore(value)}</Chip>;
}

/**
 * A V2 component reused as is (the insight clusters, run recommendations, the
 * add-to-dataset dialog, the CloudWatch source fields): it renders on the V3
 * theme through the hosted V2 skin, with the V2 toast channel it reports to.
 */
export function HostedV2({ children }: { children: ReactNode }) {
  return (
    <V2ToastProvider>
      <div className="v2 v3-host">{children}</div>
    </V2ToastProvider>
  );
}

/**
 * Render a dialog at the V3 shell root. A dialog opened from inside a panel would
 * otherwise sit in the panel's box: the reveal animation leaves a transform on
 * panels, which makes them the containing block of `position: fixed`.
 */
export function AtShell({ children }: { children: ReactNode }) {
  const root = typeof document === "undefined" ? null : document.querySelector(".v3[data-testid='v3-shell']");
  return root ? createPortal(children, root) : <>{children}</>;
}

/** One radio card of the create forms (run strategy, target, mode, data source). */
export function Option({ on, disabled, disabledReason, title, desc, badge, hint, onClick }: {
  on: boolean;
  disabled?: boolean;
  /** why it is off — shown on hover and focus (reason.tsx) */
  disabledReason?: DisabledReason | null;
  title: ReactNode;
  desc: ReactNode;
  badge?: ReactNode;
  /** a glossary definition, shown as the card's native tooltip */
  hint?: string;
  onClick: () => void;
}) {
  return (
    <Reasoned disabledReason={disabledReason}>
      <button type="button" role="radio" aria-checked={on} disabled={disabled} title={hint}
        className={on ? "v3-scenario on v3-task-opt" : "v3-scenario v3-task-opt"} onClick={onClick}>
        <b>{title}</b>
        <small>{desc}</small>
        {badge && <span className="tags">{badge}</span>}
      </button>
    </Reasoned>
  );
}

export function Field({ label, hint, children }: { label: ReactNode; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="v3-field">
      <span>{label}</span>
      {children}
      {hint && <small className="v3-hint">{hint}</small>}
    </label>
  );
}
