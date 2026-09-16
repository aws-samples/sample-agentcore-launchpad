import type { AnnouncementContent } from "./api";

/** Defense in depth for native links, including content read from old records. */
export function announcementLink(url: string): "internal" | "external" | null {
  // URL parsers normalize backslashes and silently strip some control characters.
  // Refuse them before parsing, including percent-encoded forms.
  let decoded: string;
  try {
    decoded = decodeURIComponent(url);
  } catch {
    return null;
  }
  if (/[\\\s]/u.test(url) || decoded.includes("\\") ||
    [...decoded].some((character) => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127) ||
    url !== url.trim()) return null;
  if (url.startsWith("/") && !url.startsWith("//") && !decoded.startsWith("//")) {
    return "internal";
  }
  if (!/^https:\/\//i.test(url)) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" && parsed.hostname && !parsed.username && !parsed.password
      ? "external" : null;
  } catch {
    return null;
  }
}

export function sameAnnouncementContent(a: AnnouncementContent, b: AnnouncementContent): boolean {
  return a.title === b.title && a.body === b.body &&
    (a.link_url ?? "") === (b.link_url ?? "") && (a.link_label ?? "") === (b.link_label ?? "");
}
