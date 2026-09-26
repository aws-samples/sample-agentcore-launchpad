import { ArrowRight, ExternalLink } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { AnnouncementContent } from "../../../lib/api";
import { announcementLink } from "../../../lib/announcements";

/**
 * How an announcement reads for users. User-authored content is always rendered as text
 * (no HTML/Markdown); only the explicit, validated link is clickable.
 */
export function AnnouncementPreview({ content, testId }: { content: AnnouncementContent; testId?: string }) {
  const { t } = useTranslation();
  const link = content.link_url ? announcementLink(content.link_url.trim()) : null;
  const label = content.link_label?.trim();
  return (
    <div className="v2-announcements-preview" data-testid={testId}>
      <h3>{content.title.trim() || <span className="v2-muted">{t("v2.announcements.untitled")}</span>}</h3>
      <p>{content.body.trim() || <span className="v2-muted">{t("v2.announcements.noBody")}</span>}</p>
      {link && content.link_url && label ? (
        <a
          href={content.link_url.trim()}
          target={link === "external" ? "_blank" : undefined}
          rel={link === "external" ? "noopener noreferrer" : undefined}
        >
          {label}
          {link === "external" ? (
            <>
              <ExternalLink size={13} aria-hidden="true" />
              <span className="v2-announcements-sr">{t("announcements.newTab")}</span>
            </>
          ) : (
            <ArrowRight size={13} aria-hidden="true" />
          )}
        </a>
      ) : null}
    </div>
  );
}
