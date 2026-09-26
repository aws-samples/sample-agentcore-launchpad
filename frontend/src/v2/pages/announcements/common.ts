import type { Announcement, AnnouncementContent } from "../../../lib/api";
import { announcementLink, sameAnnouncementContent } from "../../../lib/announcements";

export const PAGE_SIZE = 20;
/** Buffer key of the unsaved new draft (the URL is `?view=new`). */
export const NEW_ID = "new";
export const EMPTY: AnnouncementContent = { title: "", body: "", link_url: null, link_label: null };

export type Action = "save" | "publish" | "unpublish" | "delete" | "reload";
export type ConfirmAction = Exclude<Action, "save">;

/**
 * One local editing buffer per announcement. Buffers live in the page router, so going
 * back to the list and reopening an announcement keeps the operator's unsaved text; only
 * an explicit reload replaces it.
 */
export interface DraftSession {
  row: Announcement | null;
  content: AnnouncementContent;
  busy: Action | null;
  error: string | null;
  errorCode: string | null;
}

export function sessionFor(row: Announcement | null): DraftSession {
  return { row, content: row?.content ?? { ...EMPTY }, busy: null, error: null, errorCode: null };
}

export function isDirty(session: DraftSession): boolean {
  return !sameAnnouncementContent(session.content, session.row?.content ?? EMPTY);
}

/** Why the buffer cannot be saved (i18n key), or null. */
export function validationKey(content: AnnouncementContent): string | null {
  const linkUrl = content.link_url?.trim() ?? "";
  const linkLabel = content.link_label?.trim() ?? "";
  if (!content.title.trim() || !content.body.trim()) return "announcements.validation.required";
  if (Boolean(linkUrl) !== Boolean(linkLabel)) return "announcements.validation.linkPair";
  if (linkUrl && !announcementLink(linkUrl)) return "announcements.validation.linkUrl";
  return null;
}

export function saveReason(session: DraftSession): string | null {
  return validationKey(session.content) ?? (!isDirty(session) && session.row ? "announcements.noChanges" : null);
}

/** Publication always targets the saved snapshot the operator reviewed. */
export function publishReason(session: DraftSession): string | null {
  const { row } = session;
  if (isDirty(session)) return "announcements.saveFirst";
  if (!row) return "announcements.createFirst";
  if (row.status === "published" && !row.has_unpublished_changes) return "announcements.alreadyPublished";
  return null;
}

export function trimmed(content: AnnouncementContent): AnnouncementContent {
  return {
    title: content.title.trim(),
    body: content.body.trim(),
    link_url: content.link_url?.trim() || null,
    link_label: content.link_label?.trim() || null,
  };
}
