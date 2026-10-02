import { ExternalLink, KeyRound, RotateCcw } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type UserGrantStatus } from "../../../lib/api";
import { type AuthAsk, authCardView, pollGrant } from "../../../lib/user-grants";
import { Button, Tag } from "../../ui";

/**
 * as_user (3LO) consent card in the Chat thread. A live card links the
 * single-use authorization URL and polls the grant status until the user has
 * finished at the IdP (the `/auth/return` page binds the token), then flips to
 * authorized and enables the retry. A card restored from history can only
 * retry: the retry re-runs the prompt that triggered the ask, which asks again
 * when consent is still missing.
 */
export function AuthCard({
  ask,
  retryPrompt,
  retryDisabled,
  onRetry,
}: {
  ask: AuthAsk;
  /** the user message the retry re-sends; null when there is none */
  retryPrompt: string | null;
  retryDisabled: boolean;
  onRetry: (prompt: string) => void;
}) {
  const { t } = useTranslation();
  const live = ask.url !== null;
  const [status, setStatus] = useState<UserGrantStatus | null>(null);
  const [expired, setExpired] = useState(false);
  const view = authCardView({ live, status, expired, retryPrompt, retryDisabled });
  const authorized = view.phase === "authorized";

  useEffect(() => {
    if (!ask.agent_id) return;
    return pollGrant({
      check: () => api.userTokenStatus(ask.provider, ask.agent_id),
      live,
      onStatus: setStatus,
      onExpired: () => setExpired(true),
    });
  }, [ask.provider, ask.agent_id, live]);

  const tone = authorized ? "green" : view.phase === "expired" ? "gray" : "orange";
  const label = t(
    {
      authorized: "v2.chat.auth.authorized",
      expired: "v2.chat.auth.expiredTag",
      pending: "v2.chat.auth.pending",
      restored: "v2.chat.auth.requested",
    }[view.phase],
  );
  const vars = { tool: ask.tool || "—", connection: ask.provider };
  const body = {
    authorized: t("v2.chat.auth.authorizedBody"),
    expired: t("v2.chat.auth.expired"),
    pending: t("v2.chat.auth.body", vars),
    restored: t("v2.chat.auth.restored", vars),
  }[view.phase];

  return (
    <div className={`v2-chat-auth${authorized ? " ok" : ""}`} data-testid="auth-card" data-status={view.phase}>
      <div className="v2-chat-auth-head">
        <KeyRound size={15} aria-hidden="true" />
        <strong>{t("v2.chat.auth.title")}</strong>
        <span className="mono">{ask.provider}</span>
        <Tag tone={tone} dot={view.phase === "pending"}>
          <span data-testid="auth-card-status">{label}</span>
        </Tag>
      </div>
      <div className="v2-chat-auth-body">{body}</div>
      {ask.scopes.length > 0 && (
        <div className="v2-chat-auth-scopes">
          {t("v2.chat.auth.scopes")}
          {ask.scopes.map((s) => (
            <code key={s}>{s}</code>
          ))}
        </div>
      )}
      <div className="v2-chat-auth-actions">
        {view.showOpen && ask.url && (
          <a
            className="v2-btn primary sm"
            href={ask.url}
            target="_blank"
            rel="noopener noreferrer"
            data-testid="auth-card-open"
          >
            <ExternalLink size={13} aria-hidden="true" />
            {t("v2.chat.auth.open")}
          </a>
        )}
        {view.showOpen && (
          <span className="dim" data-testid="auth-card-waiting">
            {t("v2.chat.auth.waiting")}
          </span>
        )}
        {view.showRetry && (
          <Button
            kind={authorized ? "primary" : undefined}
            size="sm"
            disabled={!view.retryEnabled}
            title={retryPrompt ? undefined : t("v2.chat.auth.noPrompt")}
            onClick={() => retryPrompt && onRetry(retryPrompt)}
            testId="auth-card-retry"
          >
            <RotateCcw size={13} aria-hidden="true" />
            {t("v2.chat.auth.retry")}
          </Button>
        )}
      </div>
    </div>
  );
}
