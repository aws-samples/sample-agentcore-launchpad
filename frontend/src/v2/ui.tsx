// Console V2 component kit — enterprise-SaaS primitives (button, tag, filter
// select, search, table, pager, card, modal, drawer, descriptions, KPI, toast).
// Styling lives in v2.css under the `.v2` scope.
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  Inbox,
  Info,
  Loader2,
  Search,
  TriangleAlert,
  X,
} from "lucide-react";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { ToastContext } from "./hooks";

/* ---------- button ---------- */
export function Button({
  children,
  kind,
  size,
  disabled,
  title,
  onClick,
  type = "button",
  testId,
}: {
  children: ReactNode;
  kind?: "primary" | "soft" | "danger";
  size?: "sm";
  disabled?: boolean;
  title?: string;
  onClick?: () => void;
  type?: "button" | "submit";
  testId?: string;
}) {
  return (
    <button
      type={type}
      className={["v2-btn", kind ?? "", size ?? ""].join(" ").trim()}
      disabled={disabled}
      title={title}
      onClick={onClick}
      data-testid={testId}
    >
      {children}
    </button>
  );
}

export function LinkButton({
  children,
  danger,
  disabled,
  title,
  onClick,
  testId,
}: {
  children: ReactNode;
  danger?: boolean;
  disabled?: boolean;
  title?: string;
  onClick?: () => void;
  testId?: string;
}) {
  return (
    <button
      type="button"
      className={danger ? "v2-link danger" : "v2-link"}
      disabled={disabled}
      title={title}
      onClick={onClick}
      data-testid={testId}
    >
      {children}
    </button>
  );
}

/* ---------- tag ---------- */
export type TagTone = "blue" | "green" | "orange" | "red" | "gray" | "outline";

export function Tag({
  tone,
  dot,
  children,
  title,
}: {
  tone?: TagTone;
  dot?: boolean;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span className={`v2-tag ${tone ?? ""}`} title={title}>
      {dot && <span className="dot" />}
      {children}
    </span>
  );
}

/* ---------- filter select: "状态  全部 ▾" ---------- */
export interface Option {
  value: string;
  label: string;
}

export function FilterSelect({
  label,
  value,
  options,
  onChange,
  allLabel,
  testId,
}: {
  label: string;
  value: string;
  options: Option[];
  onChange: (value: string) => void;
  /** label of the "" option; omit to force a concrete value */
  allLabel?: string;
  testId?: string;
}) {
  const current = options.find((o) => o.value === value)?.label ?? allLabel ?? value;
  return (
    <label className={`v2-filter${value && allLabel ? " active" : ""}`}>
      {label}
      <b>{current}</b>
      <ChevronDown size={14} aria-hidden="true" />
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={label}
        data-testid={testId}
      >
        {allLabel !== undefined && <option value="">{allLabel}</option>}
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function SearchInput({
  value,
  onChange,
  placeholder,
  testId,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  testId?: string;
}) {
  return (
    <div className="v2-search">
      <Search size={14} aria-hidden="true" />
      <input
        className="v2-input"
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        aria-label={placeholder}
        data-testid={testId}
      />
    </div>
  );
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  ariaLabel,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
  ariaLabel?: string;
}) {
  return (
    <div className="v2-seg" role="radiogroup" aria-label={ariaLabel}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={o.value === value}
          className={o.value === value ? "on" : ""}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/* ---------- layout ---------- */
export function PageHeader({
  title,
  desc,
  tabs,
  end,
}: {
  title: string;
  desc?: ReactNode;
  tabs?: ReactNode;
  end?: ReactNode;
}) {
  return (
    <div className="v2-page-head">
      <h1>{title}</h1>
      {tabs}
      {desc && <span className="desc">{desc}</span>}
      {end && <div className="end">{end}</div>}
    </div>
  );
}

export function SubTabs<T extends string>({
  value,
  tabs,
  onChange,
}: {
  value: T;
  tabs: { value: T; label: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <div className="v2-subtabs" role="tablist">
      {tabs.map((tab) => (
        <button
          key={tab.value}
          type="button"
          role="tab"
          aria-selected={tab.value === value}
          className={tab.value === value ? "on" : ""}
          onClick={() => onChange(tab.value)}
          data-testid={`v2-tab-${tab.value}`}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

/** Back · title · (steps) · actions — header of a wizard or detail sub-page. */
export function FlowHeader({
  title,
  onBack,
  steps,
  end,
}: {
  title: ReactNode;
  onBack: () => void;
  steps?: ReactNode;
  end?: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <div className="v2-flow-head">
      <Button size="sm" onClick={onBack}>
        <ChevronLeft size={14} aria-hidden="true" />
        {t("v2.common.back")}
      </Button>
      <h1>{title}</h1>
      {steps}
      {end && <div className="end">{end}</div>}
    </div>
  );
}

export function Steps({
  steps,
  current,
  onSelect,
}: {
  steps: string[];
  current: number;
  /** clicking a step jumps back to it; forward jumps are refused */
  onSelect: (index: number) => void;
}) {
  return (
    <div className="v2-steps">
      {steps.map((label, i) => (
        <button
          key={label}
          type="button"
          className={i === current ? "on" : i < current ? "done" : ""}
          disabled={i > current}
          onClick={() => onSelect(i)}
        >
          <span className="num">{i + 1}</span>
          {label}
        </button>
      ))}
    </div>
  );
}

export function Card({
  title,
  sub,
  end,
  flush,
  children,
  testId,
}: {
  title?: ReactNode;
  sub?: ReactNode;
  end?: ReactNode;
  flush?: boolean;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <section className="v2-card" data-testid={testId}>
      <div className={flush ? "v2-card-body flush" : "v2-card-body"}>
        {title && (
          <h2 className="v2-sec-title" style={flush ? { padding: "16px 24px 0" } : undefined}>
            {title}
            {sub && <span className="sub">{sub}</span>}
            {end && <span className="end">{end}</span>}
          </h2>
        )}
        {children}
      </div>
    </section>
  );
}

export function Field({
  label,
  required,
  hint,
  error,
  full,
  children,
}: {
  label: ReactNode;
  required?: boolean;
  hint?: ReactNode;
  error?: string | null;
  full?: boolean;
  children: ReactNode;
}) {
  return (
    <div className={full ? "v2-field full" : "v2-field"}>
      <label>
        {required && <span className="req">*</span>}
        {label}
      </label>
      {children}
      {error ? <span className="err">{error}</span> : hint ? <span className="hint">{hint}</span> : null}
    </div>
  );
}

export function OptionCard({
  title,
  desc,
  on,
  disabled,
  onClick,
  badge,
  testId,
}: {
  title: string;
  desc: string;
  on: boolean;
  disabled?: boolean;
  onClick: () => void;
  badge?: ReactNode;
  testId?: string;
}) {
  return (
    <button
      type="button"
      className={on ? "v2-option on" : "v2-option"}
      disabled={disabled}
      onClick={onClick}
      aria-pressed={on}
      data-testid={testId}
    >
      <span className="t">
        {title}
        {badge}
      </span>
      <span className="d">{desc}</span>
    </button>
  );
}

export function Descriptions({
  items,
  one,
}: {
  items: { label: string; value: ReactNode }[];
  one?: boolean;
}) {
  return (
    <dl className={one ? "v2-desc one" : "v2-desc"}>
      {items.map((item) => (
        <div key={item.label}>
          <dt>{item.label}</dt>
          <dd>{item.value ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Kpi({
  label,
  value,
  sub,
  tone,
  testId,
}: {
  label: ReactNode;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "good" | "bad";
  testId?: string;
}) {
  return (
    <div className="v2-kpi" data-testid={testId}>
      <div className="l">{label}</div>
      <div className={`v ${tone ?? ""}`}>{value}</div>
      {sub && <div className="s">{sub}</div>}
    </div>
  );
}

export function Alert({
  tone = "info",
  children,
  action,
}: {
  tone?: "info" | "warn" | "error" | "success";
  children: ReactNode;
  action?: ReactNode;
}) {
  const Icon =
    tone === "error" ? AlertCircle : tone === "warn" ? TriangleAlert : tone === "success" ? CheckCircle2 : Info;
  return (
    <div className={`v2-alert ${tone}`} role={tone === "error" ? "alert" : "status"}>
      <Icon size={15} aria-hidden="true" />
      <div>{children}</div>
      {action}
    </div>
  );
}

export function Spin({ label }: { label?: string }) {
  const { t } = useTranslation();
  return (
    <div className="v2-spin" role="status">
      <Loader2 size={16} aria-hidden="true" />
      {label ?? t("v2.common.loading")}
    </div>
  );
}

/* ---------- table ---------- */
export interface Column<T> {
  key: string;
  title: ReactNode;
  render: (row: T) => ReactNode;
  className?: string;
  width?: number | string;
}

export function Table<T>({
  columns,
  rows,
  rowKey,
  loading,
  error,
  onRetry,
  empty,
  density,
  selectedKey,
  testId,
}: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  empty?: ReactNode;
  density?: "dense" | "default" | "loose";
  selectedKey?: string | null;
  testId?: string;
}) {
  const { t } = useTranslation();
  const cls = `v2-table${density && density !== "default" ? ` ${density}` : ""}`;
  return (
    <div className="v2-table-wrap">
      <table className={cls} data-testid={testId}>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} className={c.className} style={c.width ? { width: c.width } : undefined}>
                {c.title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {loading && rows.length === 0 ? (
            <tr>
              <td colSpan={columns.length}>
                <Spin />
              </td>
            </tr>
          ) : error ? (
            <tr>
              <td colSpan={columns.length}>
                <div className="v2-table-empty">
                  <AlertCircle size={28} aria-hidden="true" />
                  <div>{error}</div>
                  {onRetry && (
                    <div style={{ marginTop: 8 }}>
                      <LinkButton onClick={onRetry}>{t("v2.common.retry")}</LinkButton>
                    </div>
                  )}
                </div>
              </td>
            </tr>
          ) : rows.length === 0 ? (
            <tr>
              <td colSpan={columns.length}>
                <div className="v2-table-empty">
                  <Inbox size={32} aria-hidden="true" />
                  <div>{empty ?? t("v2.common.empty")}</div>
                </div>
              </td>
            </tr>
          ) : (
            rows.map((row) => {
              const key = rowKey(row);
              return (
                <tr key={key} className={selectedKey === key ? "selected" : undefined}>
                  {columns.map((c) => (
                    <td key={c.key} className={c.className}>
                      {c.render(row)}
                    </td>
                  ))}
                </tr>
              );
            })
          )}
        </tbody>
      </table>
    </div>
  );
}

export function Pager({
  page,
  pages,
  total,
  onPage,
}: {
  page: number;
  pages: number;
  total: number;
  onPage: (page: number) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="v2-pager">
      <span>{t("v2.common.total", { count: total })}</span>
      <button type="button" disabled={page <= 1} onClick={() => onPage(page - 1)}>
        {t("v2.common.prevPage")}
      </button>
      <span>
        {page} / {pages}
      </span>
      <button type="button" disabled={page >= pages} onClick={() => onPage(page + 1)}>
        {t("v2.common.nextPage")}
      </button>
    </div>
  );
}

/* ---------- modal / drawer ---------- */
function useEscape(onClose: () => void) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
}

export function Modal({
  title,
  open,
  onClose,
  footer,
  wide,
  children,
  testId,
}: {
  title: ReactNode;
  open: boolean;
  onClose: () => void;
  footer?: ReactNode;
  wide?: boolean;
  children: ReactNode;
  testId?: string;
}) {
  if (!open) return null;
  return (
    <ModalFrame title={title} onClose={onClose} footer={footer} wide={wide} testId={testId}>
      {children}
    </ModalFrame>
  );
}

function ModalFrame({
  title,
  onClose,
  footer,
  wide,
  children,
  testId,
}: {
  title: ReactNode;
  onClose: () => void;
  footer?: ReactNode;
  wide?: boolean;
  children: ReactNode;
  testId?: string;
}) {
  const { t } = useTranslation();
  useEscape(onClose);
  return (
    <div className="v2-mask" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={wide ? "v2-modal wide" : "v2-modal"} role="dialog" aria-modal="true" data-testid={testId}>
        <div className="v2-modal-head">
          {title}
          <button type="button" onClick={onClose} aria-label={t("v2.common.close")}>
            <X size={16} />
          </button>
        </div>
        <div className="v2-modal-body">{children}</div>
        {footer && <div className="v2-modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

export function Drawer({
  title,
  open,
  onClose,
  footer,
  children,
  testId,
}: {
  title: ReactNode;
  open: boolean;
  onClose: () => void;
  footer?: ReactNode;
  children: ReactNode;
  testId?: string;
}) {
  if (!open) return null;
  return (
    <DrawerFrame title={title} onClose={onClose} footer={footer} testId={testId}>
      {children}
    </DrawerFrame>
  );
}

function DrawerFrame({
  title,
  onClose,
  footer,
  children,
  testId,
}: {
  title: ReactNode;
  onClose: () => void;
  footer?: ReactNode;
  children: ReactNode;
  testId?: string;
}) {
  const { t } = useTranslation();
  useEscape(onClose);
  return (
    <div className="v2-mask drawer" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="v2-drawer" role="dialog" aria-modal="true" data-testid={testId}>
        <div className="v2-modal-head">
          {title}
          <button type="button" onClick={onClose} aria-label={t("v2.common.close")}>
            <X size={16} />
          </button>
        </div>
        <div className="v2-modal-body">{children}</div>
        {footer && <div className="v2-modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

/** Confirmation dialog for destructive or billable actions. */
export function Confirm({
  open,
  title,
  body,
  confirmLabel,
  danger,
  busy,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  body: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Modal
      open={open}
      title={title}
      onClose={onClose}
      testId="v2-confirm"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          <Button kind={danger ? "danger" : "primary"} disabled={busy} onClick={onConfirm} testId="v2-confirm-ok">
            {confirmLabel}
          </Button>
        </>
      }
    >
      {body}
    </Modal>
  );
}

/* ---------- toast ---------- */
interface ToastItem {
  id: number;
  tone: "success" | "error";
  text: string;
}

export function V2ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const push = useCallback((tone: "success" | "error", text: string) => {
    const id = Date.now() + Math.random();
    setItems((prev) => [...prev, { id, tone, text }]);
    window.setTimeout(() => setItems((prev) => prev.filter((i) => i.id !== id)), 3200);
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="v2-toasts" aria-live="polite">
        {items.map((item) => (
          <div key={item.id} className={`v2-toast ${item.tone}`} data-testid="v2-toast">
            {item.tone === "success" ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />}
            {item.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
