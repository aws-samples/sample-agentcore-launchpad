import type { TFunction } from "i18next";

/**
 * The ONLINE QUALITY tile's footnote — "4 sessions · 2 agents". Two counts, so two
 * i18next plural keys (`_one` / `_other`) joined by `onlineQualityFoot`; zh-CN has one
 * plural form and carries the same key set for parity (WB-002: the single template read
 * "4 sessions · 1 agents").
 */
export function onlineQualityFoot(t: TFunction, sessions: number, agents: number): string {
  return t("overview.tiles.onlineQualityFoot", {
    sessions: t("overview.tiles.onlineQualitySessions", { count: sessions }),
    agents: t("overview.tiles.onlineQualityAgents", { count: agents }),
  });
}
