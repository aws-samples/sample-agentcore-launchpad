import { useId } from "react";
import type { ButtonHTMLAttributes } from "react";

interface BtnProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  primary?: boolean;
  /**
   * Why the button is disabled, in the user's words ("Add a SKILL.md").
   *
   * Only read while `disabled` is true: the button then gets `title=reason` and
   * `aria-describedby` pointing at a small mono `.btn-hint` rendered as a sibling
   * (same weight as `.dim` helper text). When the button is enabled — or no
   * reason is given — no hint element is rendered at all. Derive the reason from
   * the same predicate that computes `disabled`; it never changes *when* the
   * button is disabled, only what the console says about it.
   */
  disabledReason?: string;
}

export function Btn({
  primary = false,
  className = "",
  children,
  disabled,
  disabledReason,
  title,
  ...rest
}: BtnProps) {
  const hintId = useId();
  const showHint = Boolean(disabled && disabledReason);
  const button = (
    <button
      {...rest}
      className={["btn", primary ? "primary" : "", className].filter(Boolean).join(" ")}
      disabled={disabled}
      title={showHint ? disabledReason : title}
      aria-describedby={showHint ? hintId : rest["aria-describedby"]}
    >
      {children}
    </button>
  );
  if (!showHint) return button;
  return (
    <>
      <span id={hintId} className="btn-hint" data-testid="btn-hint">
        {disabledReason}
      </span>
      {button}
    </>
  );
}
