import { useTranslation } from "react-i18next";

interface Props {
  total: number;
  offset: number;
  limit: number;
  busy: boolean;
  onOffset: (offset: number) => void;
}

/** These server-paged lists intentionally use fixed, bounded page sizes. */
export function AnnouncementPager({ total, offset, limit, busy, onOffset }: Props) {
  const { t } = useTranslation();
  if (total <= limit && offset === 0) return null;
  return (
    <nav className="pagerbar announcement-pager" aria-label={t("announcements.pagination")}>
      <span className="mono dim">{t("pager.total", { count: total })}</span>
      <span className="spacer" />
      <button type="button" className="fsel" disabled={busy || offset === 0}
        onClick={() => onOffset(Math.max(0, offset - limit))}>
        ‹ {t("pager.prev")}
      </button>
      <span className="mono">{t("pager.page", {
        page: Math.floor(offset / limit) + 1, pages: Math.max(1, Math.ceil(total / limit)),
      })}</span>
      <button type="button" className="fsel" disabled={busy || offset + limit >= total}
        onClick={() => onOffset(offset + limit)}>
        {t("pager.next")} ›
      </button>
    </nav>
  );
}
