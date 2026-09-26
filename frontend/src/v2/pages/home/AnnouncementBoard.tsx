import { ChevronRight, Megaphone, Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api, type PublicAnnouncement } from "../../../lib/api";
import { useLoad } from "../../hooks";
import { Alert, Button, Card, Drawer, LinkButton, Pager, Spin, Tag } from "../../ui";
import { AnnouncementPreview } from "../announcements/Preview";
import "../announcements/announcements.css";
import { readSeenAt, writeSeenAt } from "./common";

const BOARD_SIZE = 5;
const ALL_PAGE_SIZE = 10;

function useDate() {
  const { i18n } = useTranslation();
  return (iso: string, short = false) =>
    new Date(iso).toLocaleDateString(i18n.language, short ? { month: "2-digit", day: "2-digit" } : { year: "numeric", month: "short", day: "numeric" });
}

/**
 * 公告区 — installation-wide notices on the workbench: the newest one featured,
 * a few earlier ones as a list (full text in a drawer), and an "all notices"
 * drawer with paging. Items published since this browser last looked carry a
 * "new" tag. Admins get the manage entry and a publish call-to-action when empty.
 */
export function AnnouncementBoard() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const navigate = useNavigate();
  const fmt = useDate();
  const { data, loading, error, reload } = useLoad(() => api.listAnnouncements(BOARD_SIZE, 0), "announcement-board");
  // captured once per mount, so marking the board seen doesn't wipe the tags off
  // the notices the viewer is looking at right now
  const [seenAt] = useState(readSeenAt);
  const [open, setOpen] = useState<PublicAnnouncement | null>(null);
  const [showAll, setShowAll] = useState(false);
  const rows = data?.announcements ?? [];
  const total = data?.total ?? 0;
  const newest = rows[0]?.published_at;
  useEffect(() => {
    if (newest) writeSeenAt(newest);
  }, [newest]);
  const isNew = (row: PublicAnnouncement) => seenAt !== null && row.published_at > seenAt;
  const [featured, ...earlier] = rows;

  return (
    <Card
      title={t("v2.home.board.title")}
      sub={t("v2.home.board.sub")}
      testId="v2-announcement-board"
      end={
        isAdmin ? (
          <Button size="sm" onClick={() => navigate("/v2/announcements")} testId="v2-board-manage">
            {t("v2.home.board.manage")}
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
      {loading && !data && <Spin />}
      {data && total === 0 && (
        <div className="v2-home-board-empty">
          <Megaphone size={22} aria-hidden="true" />
          <p>{t("announcements.publicEmpty")}</p>
          {isAdmin && (
            <Button size="sm" kind="primary" onClick={() => navigate("/v2/announcements?view=new")}>
              <Plus size={13} aria-hidden="true" />
              {t("v2.home.board.publish")}
            </Button>
          )}
        </div>
      )}
      {featured && (
        <div className="v2-home-board-featured" data-testid={`v2-board-featured-${featured.id}`}>
          <div className="meta">
            <Tag tone="blue">{t("v2.home.board.latest")}</Tag>
            {isNew(featured) && <Tag tone="orange">{t("v2.home.board.new")}</Tag>}
            <time dateTime={featured.published_at}>{fmt(featured.published_at)}</time>
          </div>
          <AnnouncementPreview content={featured} />
        </div>
      )}
      {earlier.length > 0 && (
        <ul className="v2-home-board-list">
          {earlier.map((row) => (
            <li key={row.id}>
              <button type="button" onClick={() => setOpen(row)} data-testid={`v2-board-item-${row.id}`}>
                <time dateTime={row.published_at}>{fmt(row.published_at, true)}</time>
                <span className="title">{row.title}</span>
                {isNew(row) && <Tag tone="orange">{t("v2.home.board.new")}</Tag>}
                <ChevronRight size={14} aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      )}
      {total > BOARD_SIZE && (
        <div className="v2-home-board-more">
          <LinkButton onClick={() => setShowAll(true)} testId="v2-board-all">
            {t("v2.home.board.all", { count: total })}
          </LinkButton>
        </div>
      )}

      <Drawer title={open?.title ?? ""} open={open !== null} onClose={() => setOpen(null)} testId="v2-board-drawer">
        {open && (
          <>
            <p className="v2-muted" style={{ marginTop: 0 }}>
              {t("v2.home.board.publishedAt", { date: fmt(open.published_at) })}
            </p>
            {/* the drawer header already carries the title */}
            <div className="v2-home-board-detail">
              <AnnouncementPreview content={open} />
            </div>
          </>
        )}
      </Drawer>
      <AllAnnouncements open={showAll} onClose={() => setShowAll(false)} seenAt={seenAt} />
    </Card>
  );
}

function AllAnnouncements({ open, onClose, seenAt }: { open: boolean; onClose: () => void; seenAt: string | null }) {
  const { t } = useTranslation();
  const fmt = useDate();
  const [page, setPage] = useState(1);
  const offset = (page - 1) * ALL_PAGE_SIZE;
  const { data, loading, error, reload } = useLoad(
    () => (open ? api.listAnnouncements(ALL_PAGE_SIZE, offset) : Promise.resolve(null)),
    `announcements-all:${open}:${offset}`,
  );
  const total = data?.total ?? 0;
  return (
    <Drawer title={t("v2.home.board.allTitle")} open={open} onClose={onClose} testId="v2-board-all-drawer">
      {error && (
        <Alert tone="error">
          {error}{" "}
          <button type="button" className="v2-link" onClick={reload}>
            {t("v2.common.retry")}
          </button>
        </Alert>
      )}
      {loading && !data && <Spin />}
      <div className="v2-home-board-all">
        {(data?.announcements ?? []).map((row) => (
          <div key={row.id}>
            <div className="meta">
              <time dateTime={row.published_at}>{fmt(row.published_at)}</time>
              {seenAt !== null && row.published_at > seenAt && <Tag tone="orange">{t("v2.home.board.new")}</Tag>}
            </div>
            <AnnouncementPreview content={row} />
          </div>
        ))}
      </div>
      {total > ALL_PAGE_SIZE && <Pager page={page} pages={Math.ceil(total / ALL_PAGE_SIZE)} total={total} onPage={setPage} />}
    </Drawer>
  );
}
