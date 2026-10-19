import { type AgentForm, contextLayerSupported } from "../../../lib/agent-spec";
import type { ContextLayerAuthStatus, ContextLayerNamespace, ContextLayerStatus } from "../../../lib/api";
import type { Signal } from "../../signals";
import type { TagTone } from "../../ui";

/**
 * COA context-layer logic behind the `/v2/context-layer` page and the agent
 * wizard's context-layer section, kept free of React so visibility, the
 * namespace picker fallback and the consent polling are unit-testable.
 */

export const AUTH_TONE: Record<ContextLayerAuthStatus, TagTone> = {
  authorized: "green",
  pending: "orange",
  revoked: "gray",
  none: "outline",
};

/** The caller's own authorization; `none` while unknown or disconnected. */
export const authStatusOf = (status: ContextLayerStatus | null | undefined): ContextLayerAuthStatus =>
  status?.connected ? status.authorization?.status ?? "none" : "none";

/**
 * Whether the wizard shows the context-layer section: the workspace is connected
 * and the method can carry a layer — or the form already attaches one (an edited
 * agent keeps the section so the ref stays visible and removable).
 */
export function sectionVisible(
  status: ContextLayerStatus | null | undefined,
  form: Pick<AgentForm, "method" | "protocol" | "contextLayer">,
): boolean {
  if (!contextLayerSupported(form)) return false;
  return Boolean(status?.connected) || Boolean(form.contextLayer?.namespaceId);
}

/**
 * How the namespace is chosen. `picker` lists what COA lets the member see, which
 * needs their authorization and a successful read; anything else falls back to a
 * typed namespace id (the backend validates it is a UUID, COA checks access at
 * call time).
 */
export function pickerMode(
  auth: ContextLayerAuthStatus,
  namespaces: { data: ContextLayerNamespace[] | null; error: string | null },
): "picker" | "manual" {
  if (auth !== "authorized" || namespaces.error || !namespaces.data) return "manual";
  return "picker";
}

/** A namespace's display label: its display name, else its name, else its id. */
export const namespaceLabel = (ns: Pick<ContextLayerNamespace, "namespace_id" | "name" | "display_name">) =>
  ns.display_name || ns.name || ns.namespace_id;

/** Picker options; a stored id COA no longer lists stays selectable, labelled by
 *  its stored name. */
export function namespaceOptions(
  namespaces: ContextLayerNamespace[],
  current: { namespaceId: string; namespaceName: string } | null | undefined,
): { value: string; label: string }[] {
  const options = namespaces.map((ns) => ({ value: ns.namespace_id, label: namespaceLabel(ns) }));
  if (current?.namespaceId && !namespaces.some((ns) => ns.namespace_id === current.namespaceId)) {
    options.push({ value: current.namespaceId, label: current.namespaceName || current.namespaceId });
  }
  return options;
}

/** The form patch for a picked namespace id (its name is denormalized for the prompt). */
export function pickNamespace(
  namespaces: ContextLayerNamespace[],
  id: string,
  current: { namespaceId: string; namespaceName: string } | null | undefined,
): { namespaceId: string; namespaceName: string } | null {
  if (!id) return null;
  const ns = namespaces.find((n) => n.namespace_id === id);
  if (ns) return { namespaceId: id, namespaceName: namespaceLabel(ns) };
  return { namespaceId: id, namespaceName: current?.namespaceId === id ? current.namespaceName : "" };
}

/** One line of the deployment health list: a runtime's status string, or a yes/no
 *  flag. `null` is what the backend reports for a probe it could not run. */
export interface HealthRow {
  key: "mcp" | "serve" | "agentOauth" | "callback";
  signal: Signal;
  value: string | boolean | null;
}

export function healthRows(status: ContextLayerStatus): HealthRow[] {
  const flag = (v: boolean | null | undefined): Signal => (v == null ? "info" : v ? "ok" : "act");
  const runtime = (v: string | null | undefined): Signal => (v == null ? "info" : v === "READY" ? "ok" : "act");
  const rows: HealthRow[] = [
    { key: "mcp", signal: runtime(status.runtimes?.mcp), value: status.runtimes?.mcp ?? null },
    { key: "serve", signal: runtime(status.runtimes?.serve), value: status.runtimes?.serve ?? null },
    { key: "agentOauth", signal: flag(status.agent_oauth_enabled), value: status.agent_oauth_enabled ?? null },
  ];
  // checked for administrators only — they are the ones who can register it in COA
  if (status.can_manage) {
    rows.push({ key: "callback", signal: flag(status.callback_registered), value: status.callback_registered ?? null });
  }
  return rows;
}

/** How often an open consent tab is polled, and for how long (the server drops
 *  an unfinished consent session after 15 minutes). */
export const AUTHORIZE_POLL_MS = 3000;
export const AUTHORIZE_POLL_LIMIT_MS = 15 * 60 * 1000;

/**
 * Poll the caller's authorization after a consent tab opened, until it reads
 * `authorized` or `limitMs` elapses. A failed read keeps waiting. Returns the
 * cancel function.
 */
export function pollAuthorized(opts: {
  check: () => Promise<ContextLayerAuthStatus>;
  onAuthorized: () => void;
  onExpired?: () => void;
  intervalMs?: number;
  limitMs?: number;
  now?: () => number;
}): () => void {
  const intervalMs = opts.intervalMs ?? AUTHORIZE_POLL_MS;
  const limitMs = opts.limitMs ?? AUTHORIZE_POLL_LIMIT_MS;
  const now = opts.now ?? Date.now;
  const startedAt = now();
  let stop = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const tick = async () => {
    try {
      const status = await opts.check();
      if (stop) return;
      if (status === "authorized") {
        opts.onAuthorized();
        return;
      }
    } catch {
      if (stop) return;
    }
    if (now() - startedAt >= limitMs) {
      opts.onExpired?.();
      return;
    }
    timer = setTimeout(() => void tick(), intervalMs);
  };
  timer = setTimeout(() => void tick(), intervalMs);
  return () => {
    stop = true;
    clearTimeout(timer);
  };
}
