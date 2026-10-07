import type { AuthRequiredEvent, UserGrantInfo, UserGrantState, UserGrantStatus } from "./api";

/**
 * as_user (3LO) grant logic behind the Chat auth card and 我的授权, kept free
 * of React so the polling, retry and revoke behavior is unit-testable.
 */

/** How often a live auth card asks whether the consent has completed. */
export const AUTH_POLL_MS = 2500;
/** The server drops an unfinished consent session after 15 minutes. */
export const AUTH_POLL_LIMIT_MS = 15 * 60 * 1000;

/** The consent ask behind an auth card. A restored card has no URL: the
 *  authorization URL is single-use and never persisted. */
export type AuthAsk = Omit<AuthRequiredEvent, "url"> & { url: string | null };

/**
 * The authorization URL a card may link, or null. Only an absolute `https:`
 * URL survives: the value comes from the IdP via the agent's stream, and a
 * `javascript:`/`data:` href would run in the console's origin on click.
 */
export function safeAuthUrl(url: unknown): string | null {
  if (typeof url !== "string" || !url.trim()) return null;
  try {
    return new URL(url.trim()).protocol === "https:" ? url.trim() : null;
  } catch {
    return null;
  }
}

/** The ask carried by a live `auth_required` SSE event. A URL that is not
 *  https is dropped, so the card renders its no-URL (retry-only) state. */
export function liveAuthAsk(event: Partial<AuthRequiredEvent>, agentId: string): AuthAsk {
  return {
    provider: event.provider ?? "",
    tool: event.tool ?? "",
    scopes: event.scopes ?? [],
    url: safeAuthUrl(event.url),
    agent_id: event.agent_id || agentId,
  };
}

/** The ask behind a restored history row (`role: "auth"`, text = the
 *  Connection, name = the tool). The authorization URL is single-use and never
 *  persisted, so a restored card never links one — whatever the row carries —
 *  and can only retry. */
export function restoredAuthAsk(row: { text: string; name?: string | null }, agentId: string): AuthAsk {
  return { provider: row.text, tool: row.name ?? "", scopes: [], url: null, agent_id: agentId };
}

/** A grant is usable only once authorized with no revocation in force. */
export function effectiveStatus(state: Pick<UserGrantState, "status" | "force_reauth">): UserGrantStatus {
  if (state.status === "authorized") return state.force_reauth ? "pending" : "authorized";
  return state.status;
}

/**
 * Poll a grant's status. A live card polls every `intervalMs` until the grant
 * is usable or `limitMs` elapses (then `onExpired`); a restored card checks
 * once. A failed read keeps a live card waiting. Returns the cancel function.
 */
export function pollGrant(opts: {
  check: () => Promise<Pick<UserGrantState, "status" | "force_reauth">>;
  live: boolean;
  onStatus: (status: UserGrantStatus) => void;
  onExpired: () => void;
  intervalMs?: number;
  limitMs?: number;
  now?: () => number;
}): () => void {
  const { check, live, onStatus, onExpired } = opts;
  const intervalMs = opts.intervalMs ?? AUTH_POLL_MS;
  const limitMs = opts.limitMs ?? AUTH_POLL_LIMIT_MS;
  const now = opts.now ?? Date.now;
  const startedAt = now();
  let stop = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const tick = async () => {
    try {
      const status = effectiveStatus(await check());
      if (stop) return;
      onStatus(status);
      if (status === "authorized" || !live) return;
    } catch {
      if (stop || !live) return;
    }
    if (now() - startedAt >= limitMs) {
      onExpired();
      return;
    }
    timer = setTimeout(() => void tick(), intervalMs);
  };
  void tick();
  return () => {
    stop = true;
    clearTimeout(timer);
  };
}

export type AuthCardPhase = "pending" | "authorized" | "expired" | "restored";

/** What an auth card shows. The retry appears once waiting is over (authorized,
 *  expired, or restored from history) and needs a prompt to re-send. */
export function authCardView(opts: {
  live: boolean;
  status: UserGrantStatus | null;
  expired: boolean;
  retryPrompt: string | null;
  retryDisabled: boolean;
}): { phase: AuthCardPhase; showOpen: boolean; showRetry: boolean; retryEnabled: boolean } {
  const phase: AuthCardPhase =
    opts.status === "authorized" ? "authorized" : opts.expired ? "expired" : opts.live ? "pending" : "restored";
  const showRetry = phase !== "pending";
  return {
    phase,
    showOpen: phase === "pending",
    showRetry,
    retryEnabled: showRetry && !opts.retryDisabled && !!opts.retryPrompt,
  };
}

/** The prompt an auth card's retry re-sends: the nearest user message before
 *  it (the turn whose tool call asked for consent), or null. */
export function retryPromptFor(messages: { kind: string; text: string }[], index: number): string | null {
  for (let i = index - 1; i >= 0; i -= 1) {
    const msg = messages[i];
    if (msg.kind === "user") return msg.text.trim() ? msg.text : null;
  }
  return null;
}

/** Revoking is pointless while one is in flight or when it is already in force. */
export function revokeDisabled(grant: UserGrantInfo, mayRevoke: boolean, revoking: string | null): boolean {
  return !mayRevoke || revoking !== null || (grant.status === "revoked" && grant.force_reauth);
}

/**
 * The confirmed revoke: busy while the call runs; success closes the dialog
 * and reloads, failure reports and leaves the dialog open for another try.
 */
export async function runRevoke(
  connection: string,
  deps: {
    revoke: (connection: string) => Promise<{ provider: string }>;
    setBusy: (connection: string | null) => void;
    onDone: (provider: string) => void;
    onError: (err: unknown) => void;
  },
): Promise<boolean> {
  deps.setBusy(connection);
  try {
    const out = await deps.revoke(connection);
    deps.onDone(out.provider);
    return true;
  } catch (err) {
    deps.onError(err);
    return false;
  } finally {
    deps.setBusy(null);
  }
}
