// Glossary hints: one plain-language sentence per platform term, shown on hover
// AND keyboard focus, plus the searchable glossary dialog the shell mounts once.
// Ported from console-v3 (V2 hints + the V3 dialog). Terms, search and the
// open/close store live in ./glossaryTerms (pure, unit-tested).
import "./glossary.css";

import { BookOpen, Info } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  closeGlossary,
  GLOSSARY_TERMS,
  type GlossaryTerm,
  glossaryMatches,
  openGlossary,
  useGlossary,
} from "./glossaryTerms";
import { Modal, SearchInput } from "./ui";

/**
 * A small focusable (i) after a label. Hover or Tab onto it shows the term's
 * explanation in a `role="tooltip"` bubble that the icon references through
 * `aria-describedby`, so screen readers announce it too. A span, not a button:
 * inside a `Field`'s `<label>` a button would become the label's control.
 */
export function HintIcon({ term, label }: { term: GlossaryTerm; label?: string }) {
  const { t } = useTranslation();
  const id = useId();
  return (
    <span
      className="v2-hint"
      tabIndex={0}
      aria-label={t("glossary.whatIs", { term: label ?? t(`glossary.name.${term}`) })}
      aria-describedby={id}
      data-testid={`v2-hint-${term}`}
    >
      <Info size={13} aria-hidden="true" />
      <span role="tooltip" id={id} className="v2-hint-tip">
        {t(`glossary.${term}`)}
      </span>
    </span>
  );
}

/**
 * A term inline: dotted underline, its sentence on hover and keyboard focus, and
 * a click that opens the glossary at that term. Not for use inside a `<label>`
 * (use `HintLabel` there).
 */
export function Term({ term, children }: { term: GlossaryTerm; children?: ReactNode }) {
  const { t } = useTranslation();
  const id = useId();
  return (
    <span className="v2-hint v2-term">
      <button
        type="button"
        className="v2-term-word"
        aria-describedby={id}
        onClick={() => openGlossary(term)}
        data-testid={`v2-term-${term}`}
      >
        {children ?? t(`glossary.name.${term}`)}
      </button>
      <span role="tooltip" id={id} className="v2-hint-tip">
        {t(`glossary.${term}`)}
      </span>
    </span>
  );
}

/** A label followed by its HintIcon — the common form for `Field` labels. */
export function HintLabel({ term, children }: { term: GlossaryTerm; children: ReactNode }) {
  return (
    <>
      {children}
      <HintIcon term={term} label={typeof children === "string" ? children : undefined} />
    </>
  );
}

/** The glossary dialog, mounted once by the shell; opened through `openGlossary`. */
export function GlossaryDialog() {
  const { t, i18n } = useTranslation();
  const { open, term: focus } = useGlossary();
  const [q, setQ] = useState("");
  const findRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDListElement>(null);
  const returnTo = useRef<HTMLElement | null>(null);
  const tEn = useMemo(() => i18n.getFixedT("en"), [i18n]);

  const rows = GLOSSARY_TERMS.filter((term) =>
    glossaryMatches(term, q, t(`glossary.name.${term}`), tEn(`glossary.name.${term}`), t(`glossary.${term}`)),
  );

  // Remember what had focus, move it into the search box (a layout effect, so
  // the first typed key is not lost), and give it back on close.
  useLayoutEffect(() => {
    if (!open) return;
    returnTo.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    findRef.current?.querySelector("input")?.focus();
    return () => {
      const el = returnTo.current;
      returnTo.current = null;
      if (el?.isConnected) el.focus();
    };
  }, [open]);
  // land on the requested row
  useEffect(() => {
    if (open && focus) listRef.current?.querySelector(`[data-term="${focus}"]`)?.scrollIntoView({ block: "nearest" });
  }, [open, focus]);

  const close = useCallback(() => {
    setQ("");
    closeGlossary();
  }, []);

  return (
    <Modal
      open={open}
      onClose={close}
      wide
      testId="v2-glossary"
      title={
        <span className="v2-glossary-title">
          <BookOpen size={16} aria-hidden="true" />
          {t("v2.glossary.title")}
        </span>
      }
    >
      <div className="v2-glossary-find" ref={findRef}>
        <SearchInput value={q} onChange={setQ} placeholder={t("v2.glossary.search")} testId="v2-glossary-search" />
      </div>
      <dl className="v2-glossary" ref={listRef}>
        {rows.map((term) => (
          <div key={term} data-term={term} className={term === focus ? "on" : undefined}>
            <dt>{t(`glossary.name.${term}`)}</dt>
            <dd>{t(`glossary.${term}`)}</dd>
          </div>
        ))}
      </dl>
      {rows.length === 0 && <p className="v2-muted">{t("v2.glossary.none")}</p>}
    </Modal>
  );
}
