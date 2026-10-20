import { type AgentForm, contextLayerSupported } from "../../../lib/agent-spec";
import type {
  ContextLayerAuthStatus,
  ContextLayerDemoInfo,
  ContextLayerGatewayState,
  ContextLayerGatewayStatus,
  ContextLayerIdentity,
  ContextLayerNamespace,
  ContextLayerStatus,
} from "../../../lib/api";
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

/* ── the shared COA demo account ─────────────────────────────────────────── */

/**
 * The account a member explicitly chose for a consent started on this page,
 * remembered per browser tab (sessionStorage) so that a consent which resolved to
 * the *other* account is still noticed after a reload: COA's hosted UI consents
 * silently as whoever its browser session is signed in as.
 */
export const ACCOUNT_INTENT_KEY = "launchpad_coa_account_intent";

type IntentStore = Pick<Storage, "getItem" | "setItem" | "removeItem">;

const sessionStore = (): IntentStore | null => {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
};

export function readAccountIntent(store: IntentStore | null = sessionStore()): ContextLayerIdentity | null {
  try {
    const value = store?.getItem(ACCOUNT_INTENT_KEY);
    return value === "demo" || value === "own" ? value : null;
  } catch {
    return null;
  }
}

export function writeAccountIntent(value: ContextLayerIdentity | null, store: IntentStore | null = sessionStore()): void {
  try {
    if (value) store?.setItem(ACCOUNT_INTENT_KEY, value);
    else store?.removeItem(ACCOUNT_INTENT_KEY);
  } catch {
    /* storage unavailable: the mismatch warning just does not survive a reload */
  }
}

/** The other account. */
export const otherAccount = (account: ContextLayerIdentity): ContextLayerIdentity => (account === "demo" ? "own" : "demo");

/**
 * What the 我的授权 card shows about accounts:
 * - `choose`: not authorized — authorize with my own COA account, or with the demo one;
 * - `demo` / `own`: authorized as that account (`own` also covers unknown);
 * - `guide`: the revoke → COA sign-out → authorize again sequence toward `target`.
 *   It opens on request (`guide`, e.g. 切换为我的账号) or by itself when the consent
 *   resolved to the other account than the one chosen (`wrong`), and stays open
 *   through the revoke (`revoked`) until the target account is authorized.
 */
export type AccountView =
  | { kind: "choose" }
  | { kind: "demo" }
  | { kind: "own" }
  | { kind: "guide"; target: ContextLayerIdentity; revoked: boolean; wrong: boolean };

export function accountView(opts: {
  auth: ContextLayerAuthStatus;
  identity: ContextLayerIdentity | null | undefined;
  intent: ContextLayerIdentity | null;
  guide: ContextLayerIdentity | null;
}): AccountView {
  const authorized = opts.auth === "authorized";
  const identity = authorized ? opts.identity ?? null : null;
  const wrong = identity !== null && opts.intent !== null && identity !== opts.intent;
  const target = opts.guide ?? (wrong ? opts.intent : null);
  // an unknown identity counts as reached: there is nothing to compare with
  if (target && !(authorized && identity !== otherAccount(target))) {
    return { kind: "guide", target, revoked: !authorized, wrong };
  }
  if (!authorized) return { kind: "choose" };
  return identity === "demo" ? { kind: "demo" } : { kind: "own" };
}

/** Whether a remembered choice (intent or guide target) is fulfilled and can go. */
export function choiceSettled(
  choice: ContextLayerIdentity | null,
  auth: ContextLayerAuthStatus,
  identity: ContextLayerIdentity | null | undefined,
): boolean {
  return choice !== null && auth === "authorized" && (identity ?? null) !== otherAccount(choice);
}

/** Whether a consent card offers the demo account: the layer has one, and the ask is
 *  for the layer's own Connection (a Harness console consent or a Strands agent's). */
export const demoOffered = (ask: { provider: string }, info: ContextLayerDemoInfo | null | undefined): boolean =>
  Boolean(info?.available && info.connection && ask.provider === info.connection);

/* ── the managed COA gateway (gateway mode) ── */

/** The managed gateway's lifecycle as a signal: ready is good, settling moves on its
 *  own, failed / missing need an administrator, unknown waits for a re-read. */
export const GATEWAY_SIGNAL: Record<ContextLayerGatewayState, Signal> = {
  ready: "ok",
  creating: "info",
  failed: "act",
  missing: "act",
  unknown: "wait",
  disabled: "off",
};

/** The signal of a gateway status: `ready` with drift (`issues`) needs a repair. */
export function gatewaySignal(gw: Pick<ContextLayerGatewayStatus, "state" | "issues">): Signal {
  if (gw.state === "ready" && gw.issues.length) return "act";
  return GATEWAY_SIGNAL[gw.state] ?? "off";
}

/** Which admin actions the gateway card offers. `enable` creates it; `repair` re-runs
 *  the same idempotent enable on a broken or drifted gateway; `refresh` re-reads COA's
 *  tools into a working target; `remove` deletes it. Members get none. */
export function gatewayActions(
  gw: Pick<ContextLayerGatewayStatus, "enabled" | "state" | "issues"> | null,
  isAdmin: boolean,
): { enable: boolean; repair: boolean; refresh: boolean; remove: boolean } {
  const none = { enable: false, repair: false, refresh: false, remove: false };
  if (!isAdmin || !gw) return none;
  if (!gw.enabled) return { ...none, enable: true };
  const healthy = gw.state === "ready" && gw.issues.length === 0;
  return {
    enable: false,
    repair: !healthy && gw.state !== "creating",
    refresh: gw.state === "ready",
    remove: true,
  };
}
