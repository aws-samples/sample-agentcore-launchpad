import type { TFunction } from "i18next";

import { fmtTime } from "../../format";
import type { ApiKeyInfo } from "../../../lib/api";

/** "Created by ada · last used 2026-10-11 09:30:00" for one keys-list row; a
 *  legacy key (minted before creators were recorded) reads "unknown". */
export function apiKeyMeta(t: TFunction, key: ApiKeyInfo): string {
  const creator = t("v2.chat.keyCreator", {
    name: key.created_by_name ?? t("v2.chat.keyCreatorUnknown"),
  });
  const used = key.last_used_at
    ? t("v2.chat.keyLastUsed", { time: fmtTime(key.last_used_at) })
    : t("v2.chat.keyNeverUsed");
  return `${creator} · ${used}`;
}

/** Tooltip for the "creator offboarded" tag, or null when the creator is fine. */
export function apiKeyOffboardedNote(t: TFunction, key: ApiKeyInfo): string | null {
  return key.creator_offboarded
    ? t(`v2.chat.keyOffboardedReason.${key.creator_offboarded}`)
    : null;
}
