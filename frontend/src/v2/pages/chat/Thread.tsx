import { Database, MessagesSquare, Wrench } from "lucide-react";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

import { MessageAttachments } from "../../../components/chat/AttachmentViews";
import { TruncatedNotice } from "../../../components/chat/TruncatedNotice";
import { Markdown } from "../../../components/Markdown";
import { Alert, Spin, Tag } from "../../ui";
import { AuthCard } from "./AuthCard";
import { type ChatMessage, retryPromptFor } from "./messages";
import { PolicyDenyCard } from "./PolicyDenyCard";

export type { ChatMessage } from "./messages";

/** N17-obs507: a failed tool card whose denial card (same tool name) follows it in the same
 *  turn keeps its raw English result text collapsed — the Chinese 「被策略拦截」 card is the
 *  thing to read. */
function deniedAfter(messages: ChatMessage[], index: number): boolean {
  const tool = messages[index];
  if (tool.kind !== "tool" || tool.toolStatus !== "error") return false;
  for (let i = index + 1; i < messages.length; i++) {
    const msg = messages[i];
    if (msg.kind === "user") return false;
    if (msg.kind === "policy" && (msg.deny?.tool ?? msg.name) === tool.name) return true;
  }
  return false;
}

/** The conversation: user / agent bubbles, tool calls, consent cards, policy
 *  denials (D20), memory writes and errors. */
export function Thread({
  messages,
  userLabel,
  agentLabel,
  restoring,
  onRetry,
  retryDisabled = false,
}: {
  messages: ChatMessage[];
  userLabel: string;
  agentLabel: string;
  restoring: boolean;
  /** re-send a prompt (an auth card's retry) */
  onRetry?: (prompt: string) => void;
  retryDisabled?: boolean;
}) {
  const { t } = useTranslation();
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    ref.current?.scrollTo({ top: ref.current.scrollHeight });
  }, [messages]);

  return (
    <div className="v2-chat-thread" ref={ref} data-testid="thread">
      {restoring && messages.length === 0 && <Spin />}
      {!restoring && messages.length === 0 && (
        <div className="v2-chat-empty">
          <MessagesSquare size={36} aria-hidden="true" />
          <div>{t("v2.chat.emptyThread")}</div>
        </div>
      )}
      {messages.map((msg, i) =>
        msg.kind === "user" ? (
          <div key={i} className="v2-chat-msg user">
            <div className="v2-chat-who">{userLabel}</div>
            <div className="v2-chat-bub">
              {msg.text && <div className="v2-chat-text">{msg.text}</div>}
              <MessageAttachments files={msg.attachments} />
            </div>
          </div>
        ) : msg.kind === "agent" ? (
          <div key={i} className="v2-chat-msg agent">
            <div className="v2-chat-who">
              {agentLabel}
              {msg.streaming && (
                <Tag tone={msg.reconnecting ? "orange" : "blue"} dot>
                  {t(msg.reconnecting ? "chatPage.turnReconnecting" : "v2.chat.streaming")}
                </Tag>
              )}
            </div>
            <div className="v2-chat-bub">
              <Markdown text={msg.text} />
              {msg.streaming && <span className="v2-chat-caret" />}
              <TruncatedNotice truncated={msg.truncated} className="v2-chat-truncated" />
            </div>
          </div>
        ) : msg.kind === "tool" ? (
          <div key={i} className="v2-chat-tool" data-testid="tool-call">
            <div className="v2-chat-tool-head">
              <Wrench size={14} aria-hidden="true" />
              <span className="mono">{msg.name}</span>
              <Tag tone={msg.toolStatus === "error" ? "red" : "green"}>
                {t(msg.toolStatus === "error" ? "v2.chat.toolFailed" : "v2.chat.toolCalled")}
              </Tag>
            </div>
            {/* N16-obs85: the bounded call summary the runtime streamed (≤ 512 / 256 chars) */}
            {msg.toolInput !== undefined && (
              <div className="v2-chat-tool-detail" data-testid="tool-input">
                <span className="v2-chat-tool-label">{t("v2.chat.toolInput")}</span>
                <code>{msg.toolInput}</code>
              </div>
            )}
            {msg.toolResult !== undefined && deniedAfter(messages, i) ? (
              <details className="v2-chat-tool-detail v2-chat-policy-raw" data-testid="tool-result-collapsed">
                <summary>{t("v2.chat.toolResultDenied")}</summary>
                <code data-testid="tool-result">{msg.toolResult}</code>
              </details>
            ) : msg.toolResult !== undefined ? (
              <div className="v2-chat-tool-detail" data-testid="tool-result">
                <span className="v2-chat-tool-label">{t("v2.chat.toolResult")}</span>
                <code>{msg.toolResult}</code>
              </div>
            ) : null}
          </div>
        ) : msg.kind === "auth" && msg.auth ? (
          <AuthCard
            key={i}
            ask={msg.auth}
            retryPrompt={retryPromptFor(messages, i)}
            retryDisabled={retryDisabled || !onRetry}
            onRetry={(prompt) => onRetry?.(prompt)}
          />
        ) : msg.kind === "policy" && msg.deny ? (
          <PolicyDenyCard key={i} deny={msg.deny} />
        ) : msg.kind === "memory" ? (
          <div key={i} className="v2-chat-memline">
            <Database size={13} aria-hidden="true" />
            <span>{t("v2.chat.memorySaved")}</span>
            <code>memory.create_event</code>
          </div>
        ) : (
          <div key={i} className="v2-chat-error">
            <Alert tone="error">{msg.text}</Alert>
          </div>
        ),
      )}
    </div>
  );
}
