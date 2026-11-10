/** Accessible on/off toggle (`role="switch"`) for the enable flags of providers and models. */
export function Switch({
  on,
  disabled,
  label,
  onChange,
  testId,
}: {
  on: boolean;
  disabled?: boolean;
  label: string;
  onChange: (next: boolean) => void;
  testId?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      title={label}
      className={on ? "v2-mp-switch on" : "v2-mp-switch"}
      disabled={disabled}
      onClick={() => onChange(!on)}
      data-testid={testId}
    >
      <span className="knob" />
    </button>
  );
}
