// Console V2 component kit — enterprise-SaaS primitives (button, tag, filter
// select, search, table, pager, card, modal, drawer, descriptions, KPI, toast).
// Styling lives in v2.css under the `.v2` scope.
import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Inbox,
  Info,
  Loader2,
  Search,
  TriangleAlert,
  X,
} from "lucide-react";
import {
  type CSSProperties,
  Fragment,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTranslation } from "react-i18next";

import { dismissKey, visibleAttention } from "./attentionDismiss";
import { PageHeaderAsideContext, ToastContext, useAttentionDismissals } from "./hooks";
import { type Signal, worstSignal } from "./signals";

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
  testId,
}: {
  tone?: TagTone;
  dot?: boolean;
  children: ReactNode;
  title?: string;
  testId?: string;
}) {
  return (
    <span className={`v2-tag ${tone ?? ""}`} title={title} data-testid={testId}>
      {dot && <span className="dot" />}
      {children}
    </span>
  );
}

/* ---------- dropdowns: Select (form field) + FilterSelect ("状态  全部 ▾") ---------- */
export interface Option {
  value: string;
  label: string;
  disabled?: boolean;
  /** consecutive options sharing a group render under one header */
  group?: string;
}

/** Lists longer than this get a filter box at the top of the popup. */
const SEARCH_THRESHOLD = 8;

/**
 * Shared popup-listbox behaviour behind every V2 dropdown trigger. The popup is
 * rendered next to the trigger (not portalled: the `--v2-*` tokens live on the
 * `.v2` root) with `position: fixed`, so card / modal-body overflow never clips it.
 */
function useListbox({
  options,
  value,
  onChange,
  disabled,
  searchable,
  mono,
}: {
  options: Option[];
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  searchable?: boolean;
  mono?: boolean;
}) {
  const { t } = useTranslation();
  const listId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const typeahead = useRef({ text: "", at: 0 });
  // Space-select closes the list on keydown, so by keyup `open` is already false
  const spaceChose = useRef(false);
  const [openState, setOpen] = useState(false);
  // a control disabled while its list is open (e.g. Chat going busy) drops the list
  const open = openState && !disabled;
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(-1);
  const [pos, setPos] = useState<CSSProperties>({});
  const withSearch = searchable ?? options.length > SEARCH_THRESHOLD;
  const needle = query.trim().toLowerCase();
  const visible = useMemo(
    () => (needle ? options.filter((o) => o.label.toLowerCase().includes(needle)) : options),
    [options, needle],
  );

  const place = useCallback(() => {
    const el = triggerRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const below = window.innerHeight - r.bottom - 8;
    const above = r.top - 8;
    // flip only when the list (capped at 240 px) does not fit below
    const need = Math.min(240, options.length * 32 + (withSearch ? 44 : 0) + 10);
    const up = below < need && above > below;
    setPos({
      left: r.left,
      minWidth: r.width,
      maxWidth: Math.max(r.width, Math.min(480, window.innerWidth - r.left - 8)),
      maxHeight: Math.min(320, Math.max(120, up ? above : below)),
      ...(up ? { bottom: window.innerHeight - r.top + 4 } : { top: r.bottom + 4 }),
    });
  }, [options.length, withSearch]);

  const step = useCallback(
    (from: number, dir: 1 | -1) => {
      for (let i = from + dir; i >= 0 && i < visible.length; i += dir) {
        if (!visible[i].disabled) return i;
      }
      return from;
    },
    [visible],
  );

  const show = () => {
    if (disabled) return;
    const at = options.findIndex((o) => o.value === value && !o.disabled);
    setQuery("");
    setActive(at >= 0 ? at : options.findIndex((o) => !o.disabled));
    place();
    setOpen(true);
  };

  const close = useCallback((refocus: boolean) => {
    setOpen(false);
    if (refocus) triggerRef.current?.focus();
  }, []);

  const choose = (o: Option | undefined) => {
    if (!o || o.disabled) return;
    if (o.value !== value) onChange(o.value);
    close(true);
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (triggerRef.current?.contains(target) || popRef.current?.contains(target)) return;
      close(false);
    };
    document.addEventListener("mousedown", onDown);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    searchRef.current?.focus();
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, close, place]);

  useEffect(() => {
    if (!open || active < 0) return;
    popRef.current?.querySelector(`[data-idx="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [open, active]);

  const onKeyDown = (e: ReactKeyboardEvent<HTMLElement>) => {
    if (disabled) return;
    const inSearch = e.target === searchRef.current;
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        show();
      }
      return; // Enter / Space fall through to the trigger's click
    }
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        setActive((i) => step(i, 1));
        break;
      case "ArrowUp":
        e.preventDefault();
        setActive((i) => step(i, -1));
        break;
      case "Home":
      case "End":
        if (inSearch) return;
        e.preventDefault();
        setActive(e.key === "Home" ? step(-1, 1) : step(visible.length, -1));
        break;
      case "Enter":
        e.preventDefault();
        choose(visible[active]);
        break;
      case " ":
        if (inSearch) return;
        e.preventDefault();
        spaceChose.current = true;
        choose(visible[active]);
        break;
      case "Escape":
        // keep an enclosing Modal / Drawer (window keydown listener) open
        e.preventDefault();
        e.stopPropagation();
        close(true);
        break;
      case "Tab":
        close(false);
        break;
      default: {
        if (inSearch || e.key.length !== 1 || e.ctrlKey || e.metaKey || e.altKey) return;
        const now = Date.now();
        const ta = typeahead.current;
        ta.text = now - ta.at > 600 ? e.key.toLowerCase() : ta.text + e.key.toLowerCase();
        ta.at = now;
        const hit = visible.findIndex((o) => !o.disabled && o.label.toLowerCase().startsWith(ta.text));
        if (hit >= 0) setActive(hit);
      }
    }
  };

  const optionId = (i: number) => `${listId}-o${i}`;
  const triggerProps = {
    ref: triggerRef,
    type: "button" as const,
    disabled,
    "aria-haspopup": "listbox" as const,
    "aria-expanded": open,
    "aria-controls": open ? listId : undefined,
    "aria-activedescendant": open && !withSearch && active >= 0 ? optionId(active) : undefined,
    onClick: () => (open ? close(false) : show()),
    onKeyDown,
    onKeyUp: (e: ReactKeyboardEvent<HTMLElement>) => {
      if (e.key !== " ") return;
      if (open || spaceChose.current) e.preventDefault(); // no click-reopen after Space-select
      spaceChose.current = false;
    },
  };

  // like a native <select>, only the first option carrying the value is "the" selection
  const selectedIdx = visible.findIndex((o) => o.value === value);
  let lastGroup: string | undefined;
  const popup = open ? (
    <div
      ref={popRef}
      className={mono ? "v2-pop mono" : "v2-pop"}
      style={pos}
      onKeyDown={onKeyDown}
      // inside a <label>, a click here would re-activate the trigger and reopen the list
      onClick={(e) => e.preventDefault()}
      data-testid="v2-select-popup"
    >
      {withSearch && (
        <div className="v2-pop-search">
          <Search size={14} aria-hidden="true" />
          <input
            ref={searchRef}
            className="v2-input"
            value={query}
            placeholder={t("v2.common.searchOptions")}
            aria-label={t("v2.common.searchOptions")}
            aria-controls={listId}
            aria-activedescendant={active >= 0 ? optionId(active) : undefined}
            onChange={(e) => {
              const q = e.target.value.trim().toLowerCase();
              setQuery(e.target.value);
              setActive(options.filter((o) => !q || o.label.toLowerCase().includes(q)).findIndex((o) => !o.disabled));
            }}
          />
        </div>
      )}
      <div className="v2-pop-list" role="listbox" id={listId}>
        {visible.length === 0 && <div className="v2-pop-empty">{t("v2.common.noMatch")}</div>}
        {visible.map((o, i) => {
          const header = o.group && o.group !== lastGroup ? o.group : null;
          lastGroup = o.group;
          const selected = i === selectedIdx;
          return (
            <Fragment key={i}>
              {header && (
                <div className="v2-opt-group" role="presentation">
                  {header}
                </div>
              )}
              <div
                id={optionId(i)}
                role="option"
                aria-selected={selected}
                aria-disabled={o.disabled || undefined}
                data-idx={i}
                className={["v2-opt", o.group ? "grouped" : "", i === active ? "active" : "", selected ? "selected" : "", o.disabled ? "disabled" : ""].join(" ").trim()}
                title={o.label}
                onMouseDown={(e) => e.preventDefault()}
                onMouseEnter={() => !o.disabled && setActive(i)}
                onClick={() => choose(o)}
              >
                <span className="txt">{o.label}</span>
                {selected && <Check size={14} aria-hidden="true" />}
              </div>
            </Fragment>
          );
        })}
      </div>
    </div>
  ) : null;

  return { open, triggerProps, popup };
}

/** Form-field dropdown — the V2 replacement for `<select className="v2-select">`. */
export function Select({
  value,
  options,
  onChange,
  placeholder,
  disabled,
  mono,
  searchable,
  className,
  style,
  id,
  ariaLabel,
  testId,
}: {
  value: string;
  options: Option[];
  onChange: (value: string) => void;
  /** label of a leading, selectable "" option (the old `<option value="">`) */
  placeholder?: string;
  disabled?: boolean;
  mono?: boolean;
  /** default: on when there are more than 8 options */
  searchable?: boolean;
  className?: string;
  style?: CSSProperties;
  id?: string;
  ariaLabel?: string;
  testId?: string;
}) {
  const all = useMemo(
    () => (placeholder !== undefined ? [{ value: "", label: placeholder }, ...options] : options),
    [options, placeholder],
  );
  const { open, triggerProps, popup } = useListbox({ options: all, value, onChange, disabled, searchable, mono });
  const current = all.find((o) => o.value === value);
  return (
    <>
      <button
        {...triggerProps}
        id={id}
        className={["v2-sel", mono ? "mono" : "", open ? "open" : "", className ?? ""].join(" ").trim()}
        style={style}
        aria-label={ariaLabel}
        data-testid={testId}
      >
        <span className={current ? "val" : "val ph"}>{current?.label ?? (value || placeholder || "")}</span>
        <ChevronDown size={14} aria-hidden="true" />
      </button>
      {popup}
    </>
  );
}

export function FilterSelect({
  label,
  value,
  options,
  onChange,
  allLabel,
  disabled,
  title,
  className,
  testId,
}: {
  label: string;
  value: string;
  options: Option[];
  onChange: (value: string) => void;
  /** label of the "" option; omit to force a concrete value */
  allLabel?: string;
  disabled?: boolean;
  title?: string;
  className?: string;
  testId?: string;
}) {
  const all = useMemo(
    () => (allLabel !== undefined ? [{ value: "", label: allLabel }, ...options] : options),
    [options, allLabel],
  );
  const { open, triggerProps, popup } = useListbox({ options: all, value, onChange, disabled });
  const current = all.find((o) => o.value === value)?.label ?? (value || "—");
  return (
    <>
      <button
        {...triggerProps}
        className={["v2-filter", value && allLabel !== undefined ? "active" : "", open ? "open" : "", className ?? ""].join(" ").trim()}
        title={title}
        data-testid={testId}
      >
        {label}
        <b>{current}</b>
        <ChevronDown size={14} aria-hidden="true" />
      </button>
      {popup}
    </>
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
  /** `disabled` options cannot be picked (`title` says why); `testId` per option. */
  options: { value: T; label: string; disabled?: boolean; title?: string; testId?: string }[];
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
          disabled={o.disabled}
          title={o.title}
          data-testid={o.testId}
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
  const Aside = useContext(PageHeaderAsideContext);
  return (
    <div className="v2-page-head">
      <h1>{title}</h1>
      {tabs}
      {desc && <span className="desc">{desc}</span>}
      {(Aside || end) && (
        <div className="end">
          {Aside && <Aside />}
          {end}
        </div>
      )}
    </div>
  );
}

export function SubTabs<T extends string>({
  value,
  tabs,
  onChange,
}: {
  value: T;
  /** `title`: a native tooltip (e.g. the glossary sentence of the tab's concept) */
  tabs: { value: T; label: string; title?: string }[];
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
          title={tab.title}
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

/* ---------- signal lamp + attention panel ("what needs you", ranked) ---------- */
export function Lamp({ signal, live, title }: { signal: Signal; live?: boolean; title?: string }) {
  return (
    <span
      className="v2-lamp"
      data-signal={signal}
      data-live={live ? "true" : undefined}
      title={title}
      aria-hidden={title ? undefined : true}
    />
  );
}

export interface AttentionItem {
  key: string;
  signal: Signal;
  live?: boolean;
  title: ReactNode;
  hint?: ReactNode;
  /** right-aligned muted text, e.g. a relative time */
  meta?: ReactNode;
  /** row click; also renders the trailing "open" label */
  onOpen?: () => void;
  openLabel?: string;
  /** inline buttons, kept outside the row button so their clicks never open the row */
  actions?: ReactNode;
  testId?: string;
}

/** A ranked list of what needs a look. The page orders `items`; the card edge takes the
 *  most severe shown item's colour and `empty` is the one-line state when nothing does.
 *  With `dismissScope`, each row can be dismissed in this browser (`attentionDismiss.ts`):
 *  dismissed rows are hidden before `cap` applies, the header counts the rest, and a
 *  "Dismissed N" toggle lists them muted, each with Restore. */
export function AttentionPanel({
  title,
  items,
  empty,
  cap,
  dismissScope,
  testId,
}: {
  title: ReactNode;
  items: AttentionItem[];
  empty: ReactNode;
  /** rows shown at most, applied after the dismissed rows are hidden; the header counts
   *  every row not dismissed */
  cap?: number;
  /** turns on per-row dismiss, stored per browser under this scope (e.g. `eval.tasks`) */
  dismissScope?: string;
  testId?: string;
}) {
  const { t } = useTranslation();
  const listId = useId();
  const sectionRef = useRef<HTMLElement>(null);
  const { dismissed: dismissedKeys, dismiss, restore } = useAttentionDismissals(dismissScope);
  const [showDismissed, setShowDismissed] = useState(false);
  const view = visibleAttention(items, dismissedKeys, cap);
  const hidden = view.dismissed;
  // the last restore closes the list, so the next dismissal does not open it by itself
  if (showDismissed && hidden.length === 0) setShowDismissed(false);
  const dismissable = dismissScope !== undefined;

  // The pressed ✕ / Restore leaves with its row; focus moves to the same control of the
  // row now in that slot (else the last one, else the header toggle), so a keyboard user
  // is not dropped back to the top of the page.
  const refocus = useRef<{ selector: string; index: number } | null>(null);
  useEffect(() => {
    const target = refocus.current;
    if (!target || !sectionRef.current) return;
    refocus.current = null;
    const section = sectionRef.current;
    const same = section.querySelectorAll<HTMLButtonElement>(target.selector);
    const next =
      same[Math.min(target.index, same.length - 1)] ??
      section.querySelector<HTMLButtonElement>(".v2-attention-toggle, .v2-attention-x");
    next?.focus();
  });
  const act = (selector: string, index: number, change: () => void) => {
    refocus.current = { selector, index };
    change();
  };

  const row = (item: AttentionItem, muted: boolean, index: number) => {
    const id = dismissKey(item);
    const body = (
      <>
        <Lamp signal={item.signal} live={item.live} />
        <span className="v2-attention-text">
          <span className="title">{item.title}</span>
          {item.hint && <small>{item.hint}</small>}
        </span>
        {item.meta && <span className="v2-attention-meta">{item.meta}</span>}
        {item.onOpen && item.openLabel && (
          <span className="v2-attention-open">
            {item.openLabel}
            <ChevronRight size={14} aria-hidden="true" />
          </span>
        )}
      </>
    );
    return (
      <li
        key={item.key}
        className={dismissable ? (muted ? "v2-attention-row dismissable muted" : "v2-attention-row dismissable") : "v2-attention-row"}
        data-signal={item.signal}
        data-testid={muted && item.testId ? `${item.testId}-dismissed` : item.testId}
      >
        {item.onOpen ? (
          <button type="button" className="v2-attention-main" onClick={item.onOpen}>
            {body}
          </button>
        ) : (
          <div className="v2-attention-main">{body}</div>
        )}
        {!muted && item.actions && <span className="v2-attention-actions">{item.actions}</span>}
        {dismissable && !muted && (
          <button
            type="button"
            className="v2-attention-x"
            onClick={() => act(".v2-attention-x", index, () => dismiss(id))}
            aria-label={t("v2.attention.dismiss")}
            title={t("v2.attention.dismiss")}
            data-testid={item.testId && `${item.testId}-dismiss`}
          >
            <X size={14} aria-hidden="true" />
          </button>
        )}
        {muted && (
          <button
            type="button"
            className="v2-link v2-attention-restore"
            onClick={() => act(".v2-attention-restore", index, () => restore(id))}
            data-testid={item.testId && `${item.testId}-restore`}
          >
            {t("v2.attention.restore")}
          </button>
        )}
      </li>
    );
  };

  return (
    <section
      ref={sectionRef}
      className="v2-card v2-attention"
      data-signal={worstSignal(view.shown.map((i) => i.signal))}
      data-testid={testId}
    >
      <div className="v2-card-body">
        <h2 className="v2-sec-title">
          {title}
          {hidden.length > 0 ? (
            <span className="end v2-attention-end">
              <button
                type="button"
                className="v2-attention-toggle"
                aria-expanded={showDismissed}
                aria-controls={showDismissed ? listId : undefined}
                title={t("v2.attention.localHint")}
                onClick={() => setShowDismissed((open) => !open)}
                data-testid={testId && `${testId}-dismissed-toggle`}
              >
                {t("v2.attention.dismissedCount", { count: hidden.length })}
                <ChevronDown size={12} aria-hidden="true" />
              </button>
              <span className="v2-count">{view.remaining}</span>
            </span>
          ) : (
            <span className="end v2-count">{view.remaining}</span>
          )}
        </h2>
        {view.shown.length === 0 ? (
          <div className="v2-attention-empty">{empty}</div>
        ) : (
          <ul className="v2-attention-list">{view.shown.map((item, i) => row(item, false, i))}</ul>
        )}
        {showDismissed && hidden.length > 0 && (
          <ul id={listId} className="v2-attention-list v2-attention-dismissed" aria-label={t("v2.attention.dismissedList")}>
            {hidden.map((item, i) => row(item, true, i))}
          </ul>
        )}
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
  hint,
}: {
  title: string;
  desc: string;
  on: boolean;
  disabled?: boolean;
  onClick: () => void;
  badge?: ReactNode;
  testId?: string;
  /** a glossary sentence: a tooltip on hover/focus, wired via aria-describedby.
   *  Not a nested focusable — the card button itself is the focus target. */
  hint?: ReactNode;
}) {
  const hintId = useId();
  const classes = ["v2-option", on && "on", hint && "has-hint"].filter(Boolean).join(" ");
  return (
    <button
      type="button"
      className={classes}
      disabled={disabled}
      onClick={onClick}
      aria-pressed={on}
      aria-describedby={hint ? hintId : undefined}
      data-testid={testId}
    >
      <span className="t">
        {title}
        {hint && <Info size={13} className="v2-option-hint" aria-hidden="true" />}
        {badge}
      </span>
      <span className="d">{desc}</span>
      {hint && (
        <span role="tooltip" id={hintId} className="v2-hint-tip">
          {hint}
        </span>
      )}
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
  /** the header toggles `sort` through `onSort` (the caller sorts before paging) */
  sortable?: boolean;
}

export interface TableSort {
  key: string;
  dir: "asc" | "desc";
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
  sort,
  onSort,
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
  sort?: TableSort | null;
  onSort?: (key: string) => void;
  testId?: string;
}) {
  const { t } = useTranslation();
  const cls = `v2-table${density && density !== "default" ? ` ${density}` : ""}`;
  return (
    <div className="v2-table-wrap">
      <table className={cls} data-testid={testId}>
        <thead>
          <tr>
            {columns.map((c) => {
              const on = sort?.key === c.key ? sort.dir : null;
              const Icon = on === "asc" ? ArrowUp : on === "desc" ? ArrowDown : ArrowUpDown;
              return (
                <th
                  key={c.key}
                  className={c.className}
                  style={c.width ? { width: c.width } : undefined}
                  aria-sort={c.sortable && onSort ? (on === "asc" ? "ascending" : on === "desc" ? "descending" : "none") : undefined}
                >
                  {c.sortable && onSort ? (
                    <button type="button" className={`v2-th-sort${on ? " on" : ""}`} onClick={() => onSort(c.key)} data-testid={testId ? `${testId}-sort-${c.key}` : undefined}>
                      {c.title}
                      <Icon size={12} aria-hidden="true" />
                    </button>
                  ) : (
                    c.title
                  )}
                </th>
              );
            })}
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
  tall,
  children,
  testId,
}: {
  title: ReactNode;
  open: boolean;
  onClose: () => void;
  footer?: ReactNode;
  wide?: boolean;
  /** a long form: sits near the top and may use almost the full viewport height */
  tall?: boolean;
  children: ReactNode;
  testId?: string;
}) {
  if (!open) return null;
  return (
    <ModalFrame title={title} onClose={onClose} footer={footer} wide={wide} tall={tall} testId={testId}>
      {children}
    </ModalFrame>
  );
}

function ModalFrame({
  title,
  onClose,
  footer,
  wide,
  tall,
  children,
  testId,
}: {
  title: ReactNode;
  onClose: () => void;
  footer?: ReactNode;
  wide?: boolean;
  tall?: boolean;
  children: ReactNode;
  testId?: string;
}) {
  const { t } = useTranslation();
  useEscape(onClose);
  const modalClass = ["v2-modal", wide && "wide", tall && "tall"].filter(Boolean).join(" ");
  return (
    <div className={tall ? "v2-mask tall" : "v2-mask"} onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={modalClass} role="dialog" aria-modal="true" data-testid={testId}>
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
  wide,
  children,
  testId,
}: {
  title: ReactNode;
  open: boolean;
  onClose: () => void;
  footer?: ReactNode;
  /** media content (a video player) that needs more than the form width */
  wide?: boolean;
  children: ReactNode;
  testId?: string;
}) {
  if (!open) return null;
  return (
    <DrawerFrame title={title} onClose={onClose} footer={footer} wide={wide} testId={testId}>
      {children}
    </DrawerFrame>
  );
}

function DrawerFrame({
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
    <div className="v2-mask drawer" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={wide ? "v2-drawer wide" : "v2-drawer"} role="dialog" aria-modal="true" data-testid={testId}>
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
  confirmDisabled,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  body: ReactNode;
  confirmLabel: string;
  danger?: boolean;
  busy?: boolean;
  /** the dialog still needs an answer (e.g. a project picked) */
  confirmDisabled?: boolean;
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
          <Button kind={danger ? "danger" : "primary"} disabled={busy || confirmDisabled} onClick={onConfirm} testId="v2-confirm-ok">
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
