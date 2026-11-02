import type { RuntimeLogEvent, RuntimeLogsPage, RuntimeLogsParams } from "../../../lib/api";

export const RUNTIME_LOG_RANGES = ["1h", "6h", "24h", "7d"] as const;
export const RUNTIME_LOG_PAGE = 100;

/** Same alphabet as the backend's session-id check (observability SessionIdParam). */
const SESSION_ID_RE = /^[A-Za-z0-9_\-#:.@]{8,256}$/;

export function validSessionId(value: string): boolean {
  return SESSION_ID_RE.test(value.trim());
}

/** `?range=…` for one runtime-logs page; blank / default fields are left out. */
export function runtimeLogsQuery(params: RuntimeLogsParams): string {
  const q = new URLSearchParams();
  if (params.range) q.set("range", params.range);
  const sid = params.session_id?.trim();
  if (sid) q.set("session_id", sid);
  const keyword = params.keyword?.trim();
  if (keyword) q.set("keyword", keyword);
  if (params.include_otel) q.set("include_otel", "true");
  if (params.limit) q.set("limit", String(params.limit));
  if (params.next_token) q.set("next_token", params.next_token);
  const text = q.toString();
  return text ? `?${text}` : "";
}

/** i18n key (under `v2.agents.runtimeLogs`) explaining why nothing can be read. */
export function unavailableKey(reason: RuntimeLogsPage["unavailable_reason"]): string | null {
  switch (reason) {
    case "no_runtime":
      return "noRuntime";
    case "log_group_not_found":
      return "noLogGroup";
    case "access_denied":
      return "accessDenied";
    default:
      return null;
  }
}

/** `HH:MM:SS.mmm` in local time — the line prefix; the full stamp goes in a tooltip. */
/**
 * Why the card shows a notice instead of the log toolbar, from the agent row alone
 * (no ARN yet = no Runtime to read). A first deploy has no ARN until AWS accepted the
 * resource, so "deploying" must not read as "not hosted on AgentCore Runtime" — every
 * method that deploys (a Harness too, through its backing runtime) has logs once ready.
 */
export type RuntimeLogsGate = "deploying" | "deleted" | "notDeployed";

export function runtimeLogsGate(hosted: boolean, status: string): RuntimeLogsGate | null {
  if (status === "deleted") return "deleted";
  if (hosted) return null;
  return status === "deploying" ? "deploying" : "notDeployed";
}

export function logTime(value: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  const pad = (n: number, w = 2) => String(n).padStart(w, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

/** A stream name short enough for a line prefix: the session id, or the bracket tag. */
export function streamLabel(event: RuntimeLogEvent): string {
  if (event.session_id) return event.session_id;
  const tag = /\[([^\]]+)\]/.exec(event.stream);
  return tag ? tag[1] : event.stream;
}

/** Appends a "load more" page, dropping events already shown (same time, stream, text). */
export function appendEvents(prev: RuntimeLogEvent[], next: RuntimeLogEvent[]): RuntimeLogEvent[] {
  const seen = new Set(prev.map((e) => `${e.timestamp}|${e.stream}|${e.message}`));
  return [...prev, ...next.filter((e) => !seen.has(`${e.timestamp}|${e.stream}|${e.message}`))];
}
