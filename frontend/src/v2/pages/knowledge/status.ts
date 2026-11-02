import type { TagTone } from "../../ui";

export const KB_STATUS: Record<string, { tone: TagTone; labelKey: string }> = {
  CREATING: { tone: "blue", labelKey: "knowledge.status.creating" },
  ACTIVE: { tone: "green", labelKey: "knowledge.status.active" },
  FAILED: { tone: "red", labelKey: "knowledge.status.failed" },
  DELETING: { tone: "gray", labelKey: "knowledge.status.deleting" },
};

export const KB_STATUSES = Object.keys(KB_STATUS);

export function kbStatusLabel(t: (key: string) => string, status: string): string {
  const meta = KB_STATUS[status];
  return meta ? t(meta.labelKey) : status;
}


/** Why a KB cannot be deleted right now (a locale key), or null when it can: AWS refuses
 *  to delete a KB that is still CREATING (409 `kb.delete_conflict`), and a DELETING one
 *  is already on its way out. */
export function kbDeleteBlockedReason(status: string | null | undefined): string | null {
  const s = String(status ?? "").toUpperCase();
  if (s === "CREATING") return "v2.knowledge.deleteWhileCreating";
  if (s === "DELETING") return "story.reason.deleting";
  return null;
}
