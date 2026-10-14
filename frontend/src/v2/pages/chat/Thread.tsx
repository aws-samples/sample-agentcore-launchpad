import { Database, MessagesSquare, ThumbsDown, ThumbsUp, Wrench } from "lucide-react";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

import { MessageAttachments } from "../../../components/chat/AttachmentViews";
import { Markdown } from "../../../components/Markdown";
import type { FeedbackVerdict } from "../../../lib/api";
import { Alert, Spin, Tag } from "../../ui";
import { AuthCard } from "./AuthCard";
import { PolicyDenyCard } from "./PolicyDenyCard";
import { type ChatMessage, nextVerdict, retryPromptFor } from "./messages";

export type { ChatMessage } from "./messages";

/** The conversation: user / agent bubbles, tool calls, consent and policy-deny
 *  cards, memory writes and errors. */
export function Thread({
  messages,
  userLabel,
  agentLabel,
  restoring,
  onRate,
  onRetry,
  retryDisabled = false,
}: {
  messages: ChatMessage[];
  userLabel: string;
  agentLabel: string;
  restoring: boolean;
  /** thumbs on persisted answers; omitted ⇒ no controls. `null` withdraws. */
  onRate?: (messageId: number, verdict: FeedbackVerdict | null) => void;
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
                <Tag tone="blue" dot>
                  {t("v2.chat.streaming")}
                </Tag>
              )}
            </div>
            <div className="v2-chat-bub">
              <Markdown text={msg.text} />
              {msg.streaming && <span className="v2-chat-caret" />}
            </div>
            {onRate && msg.id != null && !msg.streaming && (
              <div className="v2-chat-thumbs" role="group" aria-label={t("v2.chat.rateAnswer")}>
                {(["up", "down"] as const).map((verdict) => {
                  const Icon = verdict === "up" ? ThumbsUp : ThumbsDown;
                  const active = msg.verdict === verdict;
                  const label = t(verdict === "up" ? "v2.chat.thumbUp" : "v2.chat.thumbDown");
                  return (
                    <button
                      key={verdict}
                      type="button"
                      className={active ? `v2-chat-thumb on ${verdict}` : "v2-chat-thumb"}
                      aria-pressed={active}
                      aria-label={label}
                      title={label}
                      onClick={() => onRate(msg.id as number, nextVerdict(msg.verdict, verdict))}
                      data-testid={`thumb-${verdict}`}
                    >
                      <Icon size={14} aria-hidden="true" />
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        ) : msg.kind === "tool" ? (
          <div key={i} className="v2-chat-tool" data-testid="tool-call">
            <Wrench size={14} aria-hidden="true" />
            <span className="mono">{msg.name}</span>
            <Tag tone="green">{t("v2.chat.toolCalled")}</Tag>
          </div>
        ) : msg.kind === "auth" && msg.auth ? (
          <AuthCard
            key={i}
            ask={msg.auth}
            retryPrompt={retryPromptFor(messages, i)}
            retryDisabled={retryDisabled || !onRetry}
            onRetry={(prompt) => onRetry?.(prompt)}
          />
        ) : msg.kind === "policy" && msg.policy ? (
          <PolicyDenyCard key={i} deny={msg.policy} />
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
