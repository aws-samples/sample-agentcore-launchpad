import { Database, MessagesSquare, Wrench } from "lucide-react";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

import { MessageAttachments } from "../../../components/chat/Attachments";
import { Markdown } from "../../../components/Markdown";
import type { ChatAttachmentMetadata } from "../../../lib/api";
import { Alert, Spin, Tag } from "../../ui";

export interface ChatMessage {
  kind: "user" | "agent" | "tool" | "memory" | "error";
  text: string;
  name?: string;
  streaming?: boolean;
  attachments?: ChatAttachmentMetadata[];
}

/** The conversation: user / agent bubbles, tool calls, memory writes and errors. */
export function Thread({
  messages,
  userLabel,
  agentLabel,
  restoring,
}: {
  messages: ChatMessage[];
  userLabel: string;
  agentLabel: string;
  restoring: boolean;
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
          </div>
        ) : msg.kind === "tool" ? (
          <div key={i} className="v2-chat-tool" data-testid="tool-call">
            <Wrench size={14} aria-hidden="true" />
            <span className="mono">{msg.name}</span>
            <Tag tone="green">{t("v2.chat.toolCalled")}</Tag>
          </div>
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
