import "../announcements.css";

import { ArrowRight, ExternalLink } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { AnnouncementPage, PublicAnnouncement } from "../../../lib/api";
import { announcementLink } from "../../../lib/announcements";
import { parseTimestamp } from "../../../lib/timestamps";
import { readSeenAt, writeSeenAt } from "../../../v2/pages/home/common";
import { Chip, Panel } from "../../ui";

/** How readers see a notice: text only, no HTML or Markdown; only the validated link is clickable. */
function Body({ row }: { row: PublicAnnouncement }) {
  const { t } = useTranslation();
  const link = row.link_url ? announcementLink(row.link_url.trim()) : null;
  const label = row.link_label?.trim();
  return (
    <>
      {row.body.trim() && <p>{row.body.trim()}</p>}
      {link && row.link_url && label && (
        <a
          href={row.link_url.trim()}
          target={link === "external" ? "_blank" : undefined}
          rel={link === "external" ? "noopener noreferrer" : undefined}
        >
          {label}
          {link === "external" ? (
            <>
              <ExternalLink size={13} aria-hidden="true" />
              <span className="sr">{t("announcements.newTab")}</span>
            </>
          ) : (
            <ArrowRight size={13} aria-hidden="true" />
          )}
        </a>
      )}
    </>
  );
}

/**
 * Installation-wide notices on the Command center (the V2 Workbench's board, in V3
 * style): the newest one in full, a few earlier ones that open in place. Notices
 * published since this browser last looked carry "New" (the same marker V2 keeps).
 * Renders nothing until loaded, on a failed read, or when nothing is published.
 * Administrators get the way to manage them.
 */
export function AnnouncementsBrief({ page, isAdmin }: { page: AnnouncementPage<PublicAnnouncement> | null; isAdmin: boolean }) {
  const { t, i18n } = useTranslation();
  // captured once per mount, so marking the board seen keeps the markers on screen now
  const [seenAt] = useState(readSeenAt);
  const rows = page?.announcements ?? [];
  const newest = rows[0]?.published_at;
  useEffect(() => {
    if (newest) writeSeenAt(newest);
  }, [newest]);
  if (rows.length === 0) return null;
  const fmt = (iso: string) =>
    parseTimestamp(iso)?.toLocaleDateString(i18n.language, { year: "numeric", month: "short", day: "numeric" }) ?? "—";
  const isNew = (row: PublicAnnouncement) => seenAt !== null && row.published_at > seenAt;
  const [featured, ...earlier] = rows;
  return (
    <div data-testid="v3-home-announcements">
      <Panel
        title={t("v2.home.board.title")}
        end={isAdmin
          ? <Link to="/v3/announcements" className="v3-btn ghost sm">{t("v2.home.board.manage")} <ArrowRight size={13} aria-hidden="true" /></Link>
          : <span>{t("v2.home.board.sub")}</span>}
      >
        <div style={{ display: "grid", gap: 10 }}>
          <div className="v3-ann-preview" data-testid={`v3-announcement-${featured.id}`}>
            <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap", fontSize: 12, color: "var(--v3-text-3)" }}>
              <Chip s="info">{t("v2.home.board.latest")}</Chip>
              {isNew(featured) && <Chip s="wait">{t("v2.home.board.new")}</Chip>}
              <time dateTime={featured.published_at}>{fmt(featured.published_at)}</time>
            </div>
            <h3>{featured.title}</h3>
            <Body row={featured} />
          </div>
          {earlier.map((row) => (
            <details key={row.id} className="v3-home-ann" data-testid={`v3-announcement-${row.id}`}>
              <summary>
                <time dateTime={row.published_at}>{fmt(row.published_at)}</time>
                <span className="title">{row.title}</span>
                {isNew(row) && <Chip s="wait">{t("v2.home.board.new")}</Chip>}
              </summary>
              <div className="v3-ann-preview"><Body row={row} /></div>
            </details>
          ))}
        </div>
      </Panel>
    </div>
  );
}
