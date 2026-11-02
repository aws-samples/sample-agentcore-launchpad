import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { Markdown } from "../../../components";
import { JumpToLatest } from "../../../components/JumpToLatest";
import type { AssistantCatalog, AssistantConversationDetail } from "../../../lib/api";
import {
  type AssistantContextDigest,
  type AssistantContextWindow,
  type AssistantLiveMessage,
  contextDividerIndex,
  retryableTurn,
  stripProposalBlock,
  TURN_RETRIED_NAME,
} from "../../../lib/assistant";
import { useFollowOwnTurns, useStickToBottom } from "../../../lib/useStickToBottom";
import { Alert, Button, Descriptions, LinkButton, Tag } from "../../ui";
import { SECTION_IDS, useReadOnly } from "./common";

export const COMPOSER_ID = "v2-assistant-input";

/** The streaming transcript, the composer and the workspace catalog summary. */
export function DiscussionCard({
  conversation, messages, input, onInput, busy, preparing, onSend, onRefreshCatalog,
  retryOf, onRetry, context, onSummarize, digest, onRegenerateDigest, regeneratingDigest,
}: {
  conversation: AssistantConversationDetail;
  messages: AssistantLiveMessage[];
  input: string;
  onInput: (text: string) => void;
  busy: boolean;
  preparing: boolean;
  onSend: () => void;
  onRefreshCatalog: () => void;
  /** the failed turn the composer will retry (null = an ordinary new turn) */
  retryOf: number | null;
  /** start (turn, prompt) or cancel (null) an edit-and-retry */
  onRetry: (target: { turn: number; prompt: string } | null) => void;
  /** the replay window the assistant sees (null = unknown, no divider) */
  context?: AssistantContextWindow | null;
  /** send the preset "summarize what is confirmed into the proposal" message */
  onSummarize?: () => void;
  /** the summary of the omitted turns the assistant sees instead (C2) */
  digest?: AssistantContextDigest | null;
  /** the owner's "wrong, regenerate" (absent for everyone else) */
  onRegenerateDigest?: () => void;
  regeneratingDigest?: boolean;
}) {
  const { t } = useTranslation();
  const { username } = useAuth();
  const readOnly = useReadOnly();
  // follows the stream only while the reader is at the bottom
  const thread = useStickToBottom();
  useFollowOwnTurns(messages.length, messages[messages.length - 1]?.role === "user", thread.toBottom);
  // A shared conversation has several senders: name everyone but the caller.
  const sender = (author?: string | null) =>
    author && author !== username ? author : t("assistantPage.you");
  const marker = t("assistantPage.proposalInText");
  const sendDisabled = busy || preparing || !input.trim() || conversation.turn_in_progress !== null;
  const retryable = busy ? null : retryableTurn(messages);
  const dividerAt = contextDividerIndex(messages, context ?? null);
  const divider = dividerAt < 0 || !context ? null : (
    <div key="context-divider" className="v2-assistant-context" role="separator" data-testid="v2-assistant-context-divider">
      <div className="line"><span>{t("v3.assistant.context.divider", { n: context.omitted_turns })}</span></div>
      <div className="v2-muted">{t("v3.assistant.context.hint")}</div>
      {!readOnly && onSummarize && (
        <button
          type="button"
          className="v2-link"
          disabled={busy || preparing || conversation.turn_in_progress !== null}
          onClick={onSummarize}
          data-testid="v2-assistant-context-summarize"
        >
          {t("v3.assistant.context.summarize")}
        </button>
      )}
      {digest?.text ? (
        <details className="v2-assistant-digest" data-testid="v2-assistant-digest">
          <summary>{t("v3.assistant.digest.show", { n: digest.through_turn ?? 0 })}</summary>
          <div className="v2-muted">{t("v3.assistant.digest.note")}</div>
          <Markdown text={digest.text} />
          {!readOnly && onRegenerateDigest && (
            <button
              type="button"
              className="v2-link"
              disabled={regeneratingDigest || digest.generating}
              onClick={onRegenerateDigest}
              data-testid="v2-assistant-digest-regenerate"
            >
              {regeneratingDigest || digest.generating
                ? t("v3.assistant.digest.regenerating")
                : t("v3.assistant.digest.regenerate")}
            </button>
          )}
        </details>
      ) : digest?.generating ? (
        <div className="v2-muted" data-testid="v2-assistant-digest-generating">{t("v3.assistant.digest.generating")}</div>
      ) : null}
    </div>
  );
  let lastErrorIndex = -1;
  if (retryable) {
    messages.forEach((m, i) => {
      if (m.role === "error" && m.turn === retryable.turn && m.name !== "proposal_rejected") lastErrorIndex = i;
    });
  }
  return (
    <section id={SECTION_IDS.discussion} className="v2-card" data-testid="v2-assistant-discussion">
      <div className="v2-card-body">
        <h2 className="v2-sec-title v2-assistant-disc-title">
          <span className="v2-assistant-disc-label">{t("v2.assistant.discussion")}</span>
          <span className="sub v2-assistant-disc-sub" title={conversation.title || undefined}>
            {conversation.title || conversation.id.slice(0, 8)}
          </span>
        </h2>
        <div
          className="v2-assistant-thread"
          ref={thread.ref}
          data-testid="v2-assistant-thread"
          data-conversation={conversation.id}
        >
          {messages.length === 0 && <div className="v2-muted" style={{ padding: "24px 0" }}>{t("assistantPage.emptyThread")}</div>}
          {messages.map((msg, i) => [i === dividerAt ? divider : null,
            msg.role === "user" ? (
              <div key={i} className="v2-turn user">
                <div className="who">{sender(msg.author)}</div>
                <div className="msg">{msg.text}</div>
              </div>
            ) : msg.role === "assistant" ? (
              <div key={i} className="v2-turn" data-testid={msg.streaming ? "v2-assistant-streaming" : undefined}>
                <div className="who">
                  {t("assistantPage.assistant")}
                  {msg.streaming && <div><Tag tone="blue">{t("assistantPage.streaming")}</Tag></div>}
                </div>
                <div className="msg">
                  <Markdown text={msg.streaming ? msg.text : stripProposalBlock(msg.text, marker)} />
                  {msg.streaming && <span className="v2-assistant-caret" />}
                </div>
              </div>
            ) : msg.role === "tool" ? (
              <div key={i} className="v2-assistant-tool" data-testid="v2-assistant-tool">
                <span className="mono">⇄ {msg.name}</span>
                <Tag tone="green">{t("assistantPage.toolCalled")}</Tag>
                {msg.text && (
                  <span className="args" title={msg.text}>
                    {msg.text.length > 220 ? msg.text.slice(0, 220) + "…" : msg.text}
                  </span>
                )}
              </div>
            ) : msg.name === TURN_RETRIED_NAME ? (
              <div key={i} className="v2-muted" style={{ fontSize: 12.5 }} data-testid="v2-assistant-turn-retried">
                {t("assistantPage.turnRetried")}
              </div>
            ) : (
              <div key={i} data-testid="v2-assistant-turn-error">
                <Alert
                  tone="error"
                  action={!readOnly && i === lastErrorIndex && retryable && retryOf === null ? (
                    <button type="button" className="v2-link" onClick={() => onRetry(retryable)} data-testid="v2-assistant-retry">
                      {t("assistantPage.editRetry")}
                    </button>
                  ) : undefined}
                >
                  {msg.name === "proposal_rejected" ? (
                    <>
                      {t("assistantPage.rejectedNote")}
                      <pre className="mono" style={{ margin: "6px 0 0", whiteSpace: "pre-wrap", fontSize: 12 }}>
                        {msg.text.split("\n").filter((l) => l.startsWith("- ")).join("\n")}
                      </pre>
                    </>
                  ) : (
                    <span className="mono">{msg.text}</span>
                  )}
                </Alert>
              </div>
            )],
          )}
          {dividerAt === messages.length && divider}
          <JumpToLatest show={!thread.atBottom} onClick={thread.toBottom} />
        </div>
        {!readOnly && <div className="v2-assistant-composer">
          <label className="v2-muted" style={{ fontSize: 12.5 }} htmlFor={COMPOSER_ID}>
            {t("assistantPage.composerLabel")}
          </label>
          <textarea
            id={COMPOSER_ID}
            className="v2-textarea"
            style={{ width: "100%", marginTop: 6 }}
            value={input}
            onChange={(e) => onInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !sendDisabled) {
                e.preventDefault();
                onSend();
              }
            }}
            placeholder={t("assistantPage.composerPlaceholder")}
            disabled={busy}
            data-testid="v2-assistant-input"
          />
          {retryOf !== null && (
            <div style={{ marginTop: 8 }} data-testid="v2-assistant-retry-note">
              <Alert
                tone="warn"
                action={<button type="button" className="v2-link" onClick={() => onRetry(null)}>{t("v2.common.cancel")}</button>}
              >
                {t("assistantPage.retryNote", { turn: retryOf })}
              </Alert>
            </div>
          )}
          <div className="bar">
            <span className="v2-muted">{t("assistantPage.discussionNote")}</span>
            <Button kind="primary" disabled={sendDisabled} onClick={onSend} testId="v2-assistant-send">
              {busy ? t("assistantPage.sending") : t("assistantPage.send")}
            </Button>
          </div>
        </div>}
        <CatalogSummary catalog={conversation.catalog} onRefresh={readOnly ? undefined : onRefreshCatalog} />
      </div>
    </section>
  );
}

function CatalogSummary({ catalog, onRefresh }: { catalog: AssistantCatalog; onRefresh?: () => void }) {
  const { t } = useTranslation();
  const names = (items: { key?: string; kb_id?: string; name?: string }[]) =>
    items.length ? items.map((i) => i.name || i.key || i.kb_id).join(", ") : t("assistantPage.catalogNone");
  const res = catalog.resources;
  return (
    <div className="v2-assistant-section" data-testid="v2-assistant-catalog">
      <h3 className="v2-row">
        {t("assistantPage.catalogTitle")}
        {onRefresh && <LinkButton onClick={onRefresh} testId="v2-assistant-catalog-refresh">{t("assistantPage.catalogRefresh")}</LinkButton>}
      </h3>
      <Descriptions
        one
        items={[
          { label: t("assistantPage.catalogTools"), value: names(catalog.tools.filter((x) => x.attachable)) },
          { label: t("assistantPage.catalogSkills"), value: names(catalog.skills) },
          {
            label: t("assistantPage.catalogKbs"),
            value: `${names(catalog.knowledge_bases)}${
              res && !(res.kb_gateway_id && res.oauth_provider_arn) ? ` · ${t("assistantPage.kbGatewayMissing")}` : ""
            }`,
          },
          ...(catalog.evaluators
            ? [{
              label: t("assistantPage.catalogEvaluators"),
              value: t("assistantPage.catalogEvaluatorsCount", {
                builtin: catalog.evaluators.filter((e) => e.source === "builtin").length,
                third: catalog.evaluators.filter((e) => e.source === "third_party").length,
              }),
            }]
            : []),
          {
            label: t("assistantPage.field.memory"),
            value: res?.memory_arn ? t("assistantPage.memoryWorkspaceAvailable") : t("assistantPage.memoryNoShared"),
          },
        ]}
      />
      {catalog.warnings.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <Alert tone="warn">{t("assistantPage.catalogWarnings", { list: catalog.warnings.join("; ") })}</Alert>
        </div>
      )}
    </div>
  );
}
