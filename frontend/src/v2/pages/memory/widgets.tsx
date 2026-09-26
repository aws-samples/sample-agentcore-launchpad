import { ChevronDown } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "../../ui";

/** Token-driven "load more" footer — renders nothing once AWS stops paginating. */
export function LoadMore({
  token,
  loading,
  onClick,
  testId,
}: {
  token: string | null;
  loading: boolean;
  onClick: () => void;
  testId?: string;
}) {
  const { t } = useTranslation();
  if (!token) return null;
  return (
    <div className="v2-memory-more">
      <Button size="sm" disabled={loading} onClick={onClick} testId={testId}>
        {loading ? t("v2.common.loading") : t("v2.memory.loadMore")}
      </Button>
    </div>
  );
}

export interface PickOption {
  value: string;
  label: string;
  disabled?: boolean;
}

/**
 * `FilterSelect` look ("标签  当前值 ▾") with per-option `disabled` and a
 * whole-control `disabled` — the kit's FilterSelect has neither, and the
 * long-term strategy pick must show unresolvable namespaces without allowing them.
 */
export function FilterPick({
  label,
  value,
  options,
  placeholder,
  disabled,
  onChange,
  testId,
}: {
  label: string;
  value: string;
  options: PickOption[];
  placeholder: string;
  disabled?: boolean;
  onChange: (value: string) => void;
  testId?: string;
}) {
  const current = options.find((o) => o.value === value)?.label ?? placeholder;
  return (
    <label className={`v2-filter${value ? " active" : ""}${disabled ? " v2-memory-disabled" : ""}`}>
      {label}
      <b className="v2-memory-pick">{current}</b>
      <ChevronDown size={14} aria-hidden="true" />
      <select
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        aria-label={label}
        data-testid={testId}
      >
        <option value="">{placeholder}</option>
        {options.map((o) => (
          <option key={o.value} value={o.value} disabled={o.disabled}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}
