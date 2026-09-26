import { useTranslation } from "react-i18next";

import type { Announcement } from "../../../lib/api";
import { Tag } from "../../ui";

/** 已发布 / 草稿, plus "has unpublished changes" and the local "unsaved" marker. */
export function StatusTags({ row, dirty }: { row: Announcement | null; dirty: boolean }) {
  const { t } = useTranslation();
  const status = row?.status ?? "draft";
  return (
    <span className="v2-tags">
      <Tag tone={status === "published" ? "green" : "gray"} dot>
        {t(`announcements.status.${status}`)}
      </Tag>
      {row?.status === "published" && row.has_unpublished_changes && (
        <Tag tone="orange">{t("announcements.unpublishedChanges")}</Tag>
      )}
      {dirty && <Tag tone="blue">{t("announcements.unsaved")}</Tag>}
    </span>
  );
}
