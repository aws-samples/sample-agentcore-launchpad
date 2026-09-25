import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import type { ChatSessionInfo } from "../../../lib/api";
import { fmtTime } from "../../format";
import { LinkButton, SearchInput, Tag } from "../../ui";

/** Past conversations with the selected agent: click to replay, END to stop the runtime session. */
export function SessionRail({
  sessions,
  currentId,
  busy,
  ending,
  endReason,
  onOpen,
  onEnd,
}: {
  sessions: ChatSessionInfo[];
  currentId: string | null;
  busy: boolean;
  ending: string | null;
  /** why END is unavailable for a row, `undefined` when it can be ended */
  endReason: (sessionId: string, ended: boolean) => string | undefined;
  onOpen: (sessionId: string) => void;
  onEnd: (session: ChatSessionInfo) => void;
}) {
  const { t } = useTranslation();
  const [query, setQuery] = useState("");
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter(
      (s) => s.preview.toLowerCase().includes(q) || s.session_id.toLowerCase().includes(q),
    );
  }, [sessions, query]);

  return (
    <>
      {sessions.length > 4 && (
        <div className="v2-chat-rail-search">
          <SearchInput value={query} onChange={setQuery} placeholder={t("v2.chat.searchSessions")} />
        </div>
      )}
      <div className="v2-chat-sessions" data-testid="history-rail">
        {shown.length === 0 && (
          <div className="v2-chat-rail-empty">
            {sessions.length ? t("v2.common.empty") : t("chatPage.historyEmpty")}
          </div>
        )}
        {shown.map((s) => {
          const reason = endReason(s.session_id, Boolean(s.ended_at));
          return (
            <div
              key={s.session_id}
              className={`v2-chat-session${s.session_id === currentId ? " on" : ""}`}
              data-ended={s.ended_at ? "true" : undefined}
            >
              <button
                type="button"
                className="v2-chat-session-open"
                disabled={busy}
                onClick={() => onOpen(s.session_id)}
                title={s.session_id}
              >
                <span className="v2-chat-session-title">{s.preview || `${s.session_id.slice(0, 20)}…`}</span>
                <span className="v2-chat-session-meta">
                  {t("chatPage.historyTurns", { count: s.turns })} · {fmtTime(s.last_at).slice(5, 16)}
                  {s.ended_at && <Tag tone="gray">{t("v2.chat.ended")}</Tag>}
                </span>
              </button>
              {!s.ended_at && (
                <LinkButton
                  danger
                  disabled={reason !== undefined}
                  title={reason}
                  onClick={() => onEnd(s)}
                  testId="history-end-session"
                >
                  {ending === s.session_id ? "…" : t("v2.chat.end")}
                </LinkButton>
              )}
            </div>
          );
        })}
      </div>
    </>
  );
}
