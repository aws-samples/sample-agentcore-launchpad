import { NAMESPACE_ID_RE } from "../../../lib/agent-spec";
import type { ContextLayerAuthStatus, ContextLayerStatus } from "../../../lib/api";
import type { Signal } from "../../signals";

/**
 * `/v2/context-layer` sub-pages are `?view=` states (the V2 convention):
 * the overview (no `view`), `view=namespace&id=<uuid>[&tab=metrics|ontology]` and
 * `view=playground[&ns=<uuid>]`. Kept free of React so the routing, the COA console
 * deep links and the empty-state rules are unit-testable.
 */

export type NamespaceTab = "sources" | "metrics" | "ontology";
export const NAMESPACE_TABS: readonly NamespaceTab[] = ["sources", "metrics", "ontology"];

export type ContextView =
  | { view: "overview" }
  | { view: "namespace"; id: string; tab: NamespaceTab }
  | { view: "playground"; ns: string | null };

const isNamespaceId = (value: string | null): value is string => !!value && NAMESPACE_ID_RE.test(value);

/** The view a URL asks for. An id that is not a UUID never reaches the backend: a
 *  namespace view without one lands on the overview, a playground without one
 *  opens with no namespace picked. */
export function contextView(params: URLSearchParams): ContextView {
  const view = params.get("view");
  if (view === "namespace") {
    const id = params.get("id");
    if (!isNamespaceId(id)) return { view: "overview" };
    const tab = params.get("tab");
    return { view: "namespace", id, tab: (NAMESPACE_TABS as readonly string[]).includes(tab ?? "") ? (tab as NamespaceTab) : "sources" };
  }
  if (view === "playground") {
    const ns = params.get("ns");
    return { view: "playground", ns: isNamespaceId(ns) ? ns : null };
  }
  return { view: "overview" };
}

/** The search params of a view (defaults omitted, so URLs stay short). */
export function viewSearch(view: ContextView): Record<string, string> {
  if (view.view === "namespace") {
    return view.tab === "sources" ? { view: "namespace", id: view.id } : { view: "namespace", id: view.id, tab: view.tab };
  }
  if (view.view === "playground") return view.ns ? { view: "playground", ns: view.ns } : { view: "playground" };
  return {};
}

/** Where in the COA console a deep link lands (COA `packages/web-app/src/App.tsx`). */
export type CoaConsoleTarget = "home" | "namespace" | "sources" | "metrics" | "ontology" | "playground";

/**
 * A deep link into the COA console, or null when the deployment reported no https
 * console URL. Authoring (sources, metrics, the ontology, grants) happens there —
 * COA forbids framing, so the console opens in a new tab.
 */
export function coaConsoleUrl(
  base: string | null | undefined,
  target: CoaConsoleTarget,
  namespaceId?: string,
): string | null {
  if (!base) return null;
  let root: URL;
  try {
    root = new URL(base);
  } catch {
    return null;
  }
  if (root.protocol !== "https:") return null;
  const origin = `${root.origin}${root.pathname.replace(/\/+$/, "")}`;
  if (target === "home") return `${origin}/`;
  if (target === "playground") return `${origin}/playground`;
  if (!namespaceId) return null;
  const ns = `${origin}/namespaces/${encodeURIComponent(namespaceId)}`;
  if (target === "namespace") return ns;
  if (target === "ontology") return `${ns}/ontology/graph`;
  return `${ns}/${target}`;
}

/** What a sub-page can show before it reads COA. */
export type ModuleGate = "loading" | "error" | "notConnected" | "needAuth" | "ready";

export function moduleGate(status: {
  data: ContextLayerStatus | null;
  loading: boolean;
  error: string | null;
}): ModuleGate {
  if (!status.data) return status.error ? "error" : "loading";
  if (!status.data.connected) return "notConnected";
  return status.data.authorization?.status === "authorized" ? "ready" : "needAuth";
}

/** The overview's namespace card: authorize first (no COA call), then the list,
 *  or why it is empty. */
export function namespacesState(
  auth: ContextLayerAuthStatus,
  list: { data: unknown[] | null; error: unknown; loading: boolean },
): "needAuth" | "loading" | "error" | "empty" | "list" {
  if (auth !== "authorized") return "needAuth";
  if (list.error) return "error";
  if (!list.data) return "loading";
  return list.data.length ? "list" : "empty";
}

/** How a COA error is presented: re-consent, first consent, no grant, gone, other. */
export type CoaErrorKind = "authorize" | "reauthorize" | "forbidden" | "notFound" | "notConnected" | "other";

export function coaErrorKind(code: string | null | undefined): CoaErrorKind {
  switch (code) {
    case "context_layer.authorization_required":
      return "authorize";
    case "context_layer.reauthorize":
      return "reauthorize";
    case "context_layer.forbidden":
      return "forbidden";
    case "context_layer.coa_not_found":
    case "http.404":
      return "notFound";
    case "context_layer.not_connected":
      return "notConnected";
    default:
      return "other";
  }
}

/* ── COA statuses → the shared signal vocabulary (v2/signals.ts) ──
 * COA's API is a preview contract: an unknown status is shown verbatim, `off`. */

const upper = (s: string | null | undefined) => (s ?? "").trim().toUpperCase();

/** A source's pipeline status (`unified-sources.smithy` `SourceStatus`). */
export function sourceSignal(status: string | null | undefined): Signal {
  const s = upper(status);
  if (["APPROVED", "COMPLETED", "ACTIVE", "READY"].includes(s)) return "ok";
  if (s.startsWith("PENDING") || s === "APPROVING") return "wait";
  if (s.startsWith("SCANNING") || ["REGISTERED", "ENRICHING", "INGESTING"].includes(s)) return "info";
  if (s.includes("FAIL") || s.includes("ERROR") || s === "REJECTED") return "act";
  return "off";
}

export function namespaceSignal(status: string | null | undefined): Signal {
  const s = upper(status);
  if (s === "ACTIVE") return "ok";
  if (s === "CREATING" || s === "UPDATING") return "info";
  if (s.includes("FAIL")) return "act";
  return "off";
}

/** A COA `/system-health` component: `ok`, `degraded` or `error: …`. */
export function healthSignal(status: string | null | undefined): Signal {
  const s = (status ?? "").trim().toLowerCase();
  if (s === "ok") return "ok";
  if (s === "degraded") return "wait";
  if (s.startsWith("error")) return "act";
  return "info";
}

/** An ontology's registry status: `ok`, `pending`, `parse_error`, `deleting`. */
export function ontologySignal(status: string | null | undefined): Signal {
  const s = (status ?? "").trim().toLowerCase();
  if (s === "ok") return "ok";
  if (s === "pending") return "info";
  if (s.includes("error")) return "act";
  return "off";
}

/** A playground trace step (`success` / `skipped` / `failed` / `running`). */
export function stepSignal(status: string | null | undefined): Signal {
  const s = (status ?? "").trim().toLowerCase();
  if (["success", "succeeded", "ok", "completed", "done", "hit"].includes(s)) return "ok";
  if (s.includes("fail") || s.includes("error") || s === "denied" || s === "blocked") return "act";
  if (["running", "started", "in_progress", "pending"].includes(s)) return "info";
  return "off";
}
