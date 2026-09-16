import { useTranslation } from "react-i18next";

import type { AnnouncementContent } from "../lib/api";
import { announcementLink } from "../lib/announcements";

/** User-authored content is always rendered as text; only the explicit link is clickable. */
export function AnnouncementContentView({ content }: { content: AnnouncementContent }) {
  const { t } = useTranslation();
  const link = content.link_url ? announcementLink(content.link_url) : null;
  return (
    <div className="announcement-content">
      <h3>{content.title}</h3>
      <p className="announcement-body">{content.body}</p>
      {link && content.link_url && content.link_label ? (
        <a
          href={content.link_url}
          target={link === "external" ? "_blank" : undefined}
          rel={link === "external" ? "noopener noreferrer" : undefined}
          className="announcement-link"
        >
          {content.link_label}
          {link === "external" ? (
            <> <span aria-hidden="true">↗</span>
              <span className="announcement-sr-only"> {t("announcements.newTab")}</span></>
          ) : <span aria-hidden="true"> →</span>}
        </a>
      ) : null}
    </div>
  );
}
