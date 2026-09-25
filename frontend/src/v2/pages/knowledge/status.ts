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

