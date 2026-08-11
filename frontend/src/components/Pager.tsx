import { useTranslation } from "react-i18next";

export const PAGE_SIZES = [20, 50, 100, 200] as const;
export const DEFAULT_PAGE_SIZE: number = PAGE_SIZES[1];

interface PagerProps {
  total: number;
  page: number; // 1-based, already clamped by the caller
  size: number;
  onPage: (page: number) => void;
  onSize: (size: number) => void;
  /** Render the bar even when one page suffices (controls disable themselves). */
  always?: boolean;
}

/** Table footer pagination — hidden entirely while one page suffices, unless
 *  `always` keeps it visible for consistency across sibling tables.
 *  Shared by the Observability tabs and the Evaluation module tables. */
export function Pager({ total, page, size, onPage, onSize, always }: PagerProps) {
  const { t } = useTranslation();
  const pages = Math.max(1, Math.ceil(total / size));
  if (!always && total <= PAGE_SIZES[0]) return null;
  return (
    <div className="pagerbar">
      <span className="mono dim">{t("pager.total", { count: total })}</span>
      <span className="spacer" />
      <button className="fsel" disabled={page <= 1} onClick={() => onPage(page - 1)}>
        ‹ {t("pager.prev")}
      </button>
      <span className="mono">{t("pager.page", { page, pages })}</span>
      <button className="fsel" disabled={page >= pages} onClick={() => onPage(page + 1)}>
        {t("pager.next")} ›
      </button>
      <select
        className="fsel"
        value={size}
        onChange={(e) => onSize(Number(e.target.value))}
        aria-label={t("pager.sizeLabel")}
      >
        {PAGE_SIZES.map((s) => (
          <option key={s} value={s}>
            {t("pager.perPage", { size: s })}
          </option>
        ))}
      </select>
    </div>
  );
}
