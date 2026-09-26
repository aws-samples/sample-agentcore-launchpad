import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api } from "../../../lib/api";
import { useLoad } from "../../hooks";
import { Alert, Button, Card, Pager, Spin } from "../../ui";
import { AnnouncementPreview } from "../announcements/Preview";
import "../announcements/announcements.css";

const PAGE_SIZE = 3;

/**
 * Published announcements on the V2 home — the V2 twin of the classic Overview
 * feed. Installation-wide, independent of the selected workspace.
 */
export function AnnouncementFeed() {
  const { t, i18n } = useTranslation();
  const { isAdmin } = useAuth();
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const offset = (page - 1) * PAGE_SIZE;
  const { data, loading, error, reload } = useLoad(() => api.listAnnouncements(PAGE_SIZE, offset), `announcements:${offset}`);
  const total = data?.total ?? 0;
  // an announcement withdrawn under us can leave the page past the end
  useEffect(() => {
    if (data && offset > 0 && offset >= data.total) setPage(Math.max(1, Math.ceil(data.total / PAGE_SIZE)));
  }, [data, offset]);
  // stay out of the way until there is something to read (admins still get the manage link)
  if (!isAdmin && !loading && !error && total === 0) return null;

  return (
    <Card
      title={t("announcements.feedTitle")}
      testId="v2-announcement-feed"
      end={
        isAdmin ? (
          <Button size="sm" onClick={() => navigate("/v2/announcements")}>
            {t("announcements.manage")}
          </Button>
        ) : undefined
      }
    >
      {error && (
        <Alert tone="error">
          {error}{" "}
          <button type="button" className="v2-link" onClick={reload}>
            {t("v2.common.retry")}
          </button>
        </Alert>
      )}
      {loading && !data ? <Spin /> : null}
      {data && total === 0 ? <p className="v2-muted">{t("announcements.publicEmpty")}</p> : null}
      <div style={{ display: "grid", gap: 12 }}>
        {(data?.announcements ?? []).map((row) => (
          <div key={row.id} data-testid={`v2-announcement-${row.id}`}>
            <time dateTime={row.published_at} className="v2-muted" style={{ fontSize: 12 }}>
              {new Date(row.published_at).toLocaleDateString(i18n.language, { year: "numeric", month: "short", day: "numeric" })}
            </time>
            <AnnouncementPreview content={row} />
          </div>
        ))}
      </div>
      {total > PAGE_SIZE && <Pager page={page} pages={Math.ceil(total / PAGE_SIZE)} total={total} onPage={setPage} />}
    </Card>
  );
}
