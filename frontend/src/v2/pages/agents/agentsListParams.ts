import type { AgentInfo } from "../../../lib/api";

/**
 * The V2 agents list keeps its filters in the URL so a detail round-trip, a reload
 * or a shared link lands on the same filtered list. `""` means "all" and is never
 * written. The list has no sort control (rows keep the API's newest-first order), so
 * there is no `sort` param.
 *
 * Note `method` doubles as the wizard's prefill param under `?view=new`, so these
 * keys are carried into `view=detail|identity` but never into `view=new`.
 */
export interface AgentsListFilters {
  q: string;
  status: AgentInfo["status"] | "";
  method: AgentInfo["method"] | "";
}

export const LIST_DEFAULTS: AgentsListFilters = { q: "", status: "", method: "" };

const LIST_KEYS = ["q", "status", "method"] as const;

const STATUSES: readonly AgentInfo["status"][] = ["draft", "deploying", "active", "failed", "deleted"];
const METHODS: readonly AgentInfo["method"][] = [
  "harness",
  "zip_runtime",
  "container",
  "studio",
  "byoc",
  "discovered_runtime",
];

function pick<T extends string>(allowed: readonly T[], value: string | null): T | "" {
  return allowed.find((v) => v === value) ?? "";
}

/** Filters from the URL; an unknown status or method falls back to "all". */
export function parseListParams(params: URLSearchParams): AgentsListFilters {
  return {
    q: params.get("q") ?? "",
    status: pick(STATUSES, params.get("status")),
    method: pick(METHODS, params.get("method")),
  };
}

/** A copy of `prev` with the filters written (defaults removed); other params such as
 *  `view` / `id` are kept as they are. */
export function writeListParams(prev: URLSearchParams, filters: AgentsListFilters): URLSearchParams {
  const next = new URLSearchParams(prev);
  for (const key of LIST_KEYS) {
    const value = filters[key];
    if (value) next.set(key, value);
    else next.delete(key);
  }
  return next;
}

/** Only the list's own (valid, non-default) filter params: where Back from a sub-view goes. */
export function listOnly(params: URLSearchParams): URLSearchParams {
  return writeListParams(new URLSearchParams(), parseListParams(params));
}

/** A sub-view (`detail`, `identity`) of one agent that keeps the list filters, so its
 *  Back returns to the same filtered list. */
export function withView(params: URLSearchParams, view: string, id: string): URLSearchParams {
  return writeListParams(new URLSearchParams({ view, id }), parseListParams(params));
}
