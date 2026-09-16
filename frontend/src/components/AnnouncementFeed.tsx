import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { api, errorMessage, type AnnouncementPage, type PublicAnnouncement } from "../lib/api";
import { AnnouncementContentView } from "./AnnouncementContentView";
import { AnnouncementPager } from "./AnnouncementPager";
import { LoadError } from "./LoadError";
import { Panel } from "./Panel";
import "./announcements.css";

const PAGE_SIZE = 3;

/** Independent of AWS metrics and the workspace: announcements are installation-wide. */
export function AnnouncementFeed() {
  const { t, i18n } = useTranslation();
  const { isAdmin } = useAuth();
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [data, setData] = useState<(AnnouncementPage<PublicAnnouncement> & { offset: number }) | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    api.listAnnouncements(PAGE_SIZE, offset, controller.signal)
      .then((page) => {
        if (controller.signal.aborted) return;
        if (offset > 0 && offset >= page.total) {
          setOffset(Math.max(0, Math.floor((page.total - 1) / PAGE_SIZE) * PAGE_SIZE));
          return;
        }
        setData({ ...page, offset });
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setError(errorMessage(err));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [offset, refresh]);

  const current = data?.offset === offset ? data : null;
  return (
    <Panel className="announcement-feed" title={t("announcements.feedTitle")}
      data-testid="announcement-feed" aria-busy={loading} pad={false}
      end={isAdmin ? <Link to="/announcements">{t("announcements.manage")} →</Link> : undefined}>
      {error ? <LoadError message={error} onRetry={() => setRefresh((v) => v + 1)}
        data-testid="announcements-public-error" /> : null}
      {loading ? <div className="loading-line" role="status">{t("common.loading")}</div> : null}
      {current?.announcements.map((row) => (
        <article className="announcement-notice" key={row.id} data-testid={`announcement-${row.id}`}>
          <time dateTime={row.published_at} className="announcement-date">
            {new Date(row.published_at).toLocaleDateString(i18n.language, {
              year: "numeric", month: "short", day: "numeric",
            })}
          </time>
          <AnnouncementContentView content={row} />
        </article>
      ))}
      {!loading && !error && current?.total === 0 ? (
        <div className="empty">{t("announcements.publicEmpty")}</div>
      ) : null}
      <AnnouncementPager total={current?.total ?? data?.total ?? 0} offset={offset}
        limit={PAGE_SIZE} busy={loading} onOffset={setOffset} />
    </Panel>
  );
}
