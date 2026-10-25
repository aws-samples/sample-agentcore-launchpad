// Pure helpers for the Command center modules carried over from the V2 Workbench
// (registry assets, pass rate, online quality, TTFA, inbox, lifecycle, service health).
import type { TFunction } from "i18next";

import type { InboxItem, OverviewInfo } from "../../../lib/api";
import { SERVICES } from "../../../v2/pages/home/common";
import { v3TwinOf } from "../../nav";
import type { Signal } from "../../ui";

/** The Agent lifecycle, each step on its V3 page. */
export const LIFECYCLE = [
  { key: "create", to: "/v3/create" },
  { key: "chat", to: "/v3/chat" },
  { key: "observe", to: "/v3/observability" },
  { key: "evaluate", to: "/v3/tasks?view=new" },
  { key: "optimize", to: "/v3/experiments" },
  { key: "govern", to: "/v3/governance" },
] as const;

/** A TTFA span as minutes (< 1 h), hours (< 48 h) or days — V2 Workbench's reading. */
export function fmtSpan(t: TFunction, seconds: number): string {
  const minutes = seconds / 60;
  if (minutes < 60) return t("v2.home.kpi.minutes", { n: Math.max(1, Math.round(minutes)) });
  const hours = minutes / 60;
  if (hours < 48) return t("v2.home.kpi.hours", { n: Math.round(hours * 10) / 10 });
  return t("v2.home.kpi.days", { n: Math.round((hours / 24) * 10) / 10 });
}

/** Online quality on the signal scale: ≥ 80 % fine, < 50 % needs you, else no colour. */
export function qualitySignal(mean: number | null | undefined): Signal | undefined {
  if (mean == null) return undefined;
  if (mean >= 0.8) return "ok";
  if (mean < 0.5) return "act";
  return undefined;
}

const INBOX_SIGNAL: Record<InboxItem["severity"], Signal> = { action: "act", warn: "wait", info: "info" };

export function inboxSignal(severity: InboxItem["severity"]): Signal {
  return INBOX_SIGNAL[severity] ?? "info";
}

export interface ServiceRow {
  id: (typeof SERVICES)[number]["id"];
  to: string;
  /** ready · "usage" not used yet (normal on a fresh account) · "bootstrap" missing */
  state: "ready" | "idle" | "pending";
  signal: Signal;
  detail: string;
}

/** The V3 page a service row opens: its V2 module's twin, the agent list for Runtime. */
function serviceLink(to: string): string {
  if (to === "/v2/agents") return "/v3/agents";
  return v3TwinOf(to, "") ?? to;
}

/** One row per AgentCore service, as the V2 Workbench's service health reads it. */
export function serviceRows(info: OverviewInfo, activeAgents: number | null): ServiceRow[] {
  return SERVICES.map(({ id, kind, to }) => {
    const ready = id === "runtime" ? (activeAgents ?? 0) > 0 : Boolean(info.services[id]);
    const state: ServiceRow["state"] = ready ? "ready" : kind === "usage" ? "idle" : "pending";
    return {
      id,
      to: serviceLink(to),
      state,
      signal: state === "ready" ? "ok" : state === "idle" ? "off" : "wait",
      detail: id === "runtime" ? "" : (info.service_detail[id] ?? ""),
    };
  });
}
