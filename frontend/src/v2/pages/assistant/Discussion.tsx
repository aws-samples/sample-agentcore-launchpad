import { Fragment, type RefObject } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { Markdown } from "../../../components";
import type { AssistantCatalog, AssistantConversationDetail } from "../../../lib/api";
import {
  type AssistantLiveMessage, AUTOCORRECT_NAMES, isNoticeRow, MISSING_NAMES, PROPOSAL_AUTOCORRECT_FAILED,
  PROPOSAL_AUTOCORRECT_PENDING, PROPOSAL_MISSING_PENDING, PROPOSAL_MISSING_RECOVERED,
  PROPOSAL_REJECTED_NAME, retryableTurn, stripProposalBlock, TURN_RETRIED_NAME, catalogMemoryKind, TURN_INTERRUPTED_NAME,
} from "../../../lib/assistant";
import { errorDetails } from "../../../lib/validation-errors";
import { Alert, Button, Descriptions, LinkButton, Tag } from "../../ui";
import { SECTION_IDS, useReadOnly } from "./common";
import { ValidationErrorList } from "./ValidationErrors";

export const COMPOSER_ID = "v2-assistant-input";

/** The streaming transcript, the composer and the workspace catalog summary. */
export function DiscussionCard({
  conversation, messages, input, onInput, busy, preparing, onSend, onRefreshCatalog, threadRef,
  retryOf, onRetry,
}: {
  conversation: AssistantConversationDetail;
  messages: AssistantLiveMessage[];
  input: string;
  onInput: (text: string) => void;
  busy: boolean;
  preparing: boolean;
  onSend: () => void;
  onRefreshCatalog: () => void;
  threadRef: RefObject<HTMLDivElement>;
  /** the failed turn the composer will retry (null = an ordinary new turn) */
  retryOf: number | null;
  /** start (turn, prompt) or cancel (null) an edit-and-retry */
  onRetry: (target: { turn: number; prompt: string } | null) => void;
}) {
  const { t } = useTranslation();
  const { username } = useAuth();
  // A read-only viewer (an administrator on another member's unshared conversation)
  // reads the transcript and tails a live turn; it never sends, retries or refreshes.
  const readOnly = useReadOnly();
  // A shared conversation has several senders: name everyone but the caller.
  const sender = (author?: string | null) =>
    author && author !== username ? author : t("assistantPage.you");
  const marker = t("assistantPage.proposalInText");
  const sendDisabled = busy || preparing || !input.trim() || conversation.turn_in_progress !== null;
  const retryable = busy ? null : retryableTurn(messages);
  let lastErrorIndex = -1;
  if (retryable) {
    messages.forEach((m, i) => {
      if (m.role === "error" && m.turn === retryable.turn && !isNoticeRow(m)) lastErrorIndex = i;
    });
  }
  // N17-obs468: every answered turn says whether it produced a revision (from the stored
  // transcript; rows streamed in this tab get theirs on the reload after the turn), so a
  // reply claiming a change it never made is visible
  const turnRevisions = conversation.turn_revisions;
  const lastRowOfTurn = new Map<number, number>();
  messages.forEach((m, i) => {
    if (typeof m.turn === "number" && m.role !== "user") lastRowOfTurn.set(m.turn, i);
  });
  const turnLine = (msg: AssistantLiveMessage, i: number) => {
    if (!turnRevisions || typeof msg.turn !== "number" || lastRowOfTurn.get(msg.turn) !== i) return null;
    const produced = turnRevisions[String(msg.turn)];
    if (!produced) return null;
    return (
      <div className="v2-assistant-turn-rev v2-muted" data-testid="v2-assistant-turn-revision" data-turn={msg.turn}>
        {produced.length
          ? t("assistantPage.turnRevision", { revision: produced[produced.length - 1] })
          : t("assistantPage.turnNoRevision")}
      </div>
    );
  };
  return (
    <section id={SECTION_IDS.discussion} className="v2-card" data-testid="v2-assistant-discussion">
      <div className="v2-card-body">
        <h2 className="v2-sec-title">
          {t("v2.assistant.discussion")}
          <span className="sub">{conversation.title || conversation.id.slice(0, 8)}</span>
        </h2>
        <div
          className="v2-assistant-thread"
          ref={threadRef}
          data-testid="v2-assistant-thread"
          data-conversation={conversation.id}
        >
          {messages.length === 0 && <div className="v2-muted" style={{ padding: "24px 0" }}>{t("assistantPage.emptyThread")}</div>}
          {messages.map((msg, i) => <Fragment key={i}>{
            msg.role === "user" ? (
              <div key={i} className="v2-turn user">
                <div className="who">{sender(msg.author)}</div>
                <div className="msg">{msg.text}</div>
              </div>
            ) : msg.role === "assistant" ? (
              <div key={i} className="v2-turn" data-testid={msg.streaming ? (msg.reconnecting ? "v2-assistant-reconnecting" : "v2-assistant-streaming") : undefined}>
                <div className="who">
                  {t("assistantPage.assistant")}
                  {msg.streaming && (
                    <div>
                      <Tag tone={msg.reconnecting ? "orange" : "blue"}>
                        {t(msg.reconnecting ? "assistantPage.turnReconnecting" : "assistantPage.streaming")}
                      </Tag>
                    </div>
                  )}
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
            ) : AUTOCORRECT_NAMES.includes(msg.name ?? "") ? (
              <AutocorrectNotice key={i} msg={msg} />
            ) : MISSING_NAMES.includes(msg.name ?? "") ? (
              <MissingNotice key={i} msg={msg} />
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
                  {msg.name === PROPOSAL_REJECTED_NAME ? (
                    <>
                      {t("assistantPage.rejectedNote")}
                      <ValidationErrorList errors={errorDetails(noteLines(msg.text), msg.errors)} testId="v2-assistant-rejected-errors" />
                    </>
                  ) : msg.name === TURN_INTERRUPTED_NAME ? (
                    <span data-testid="v2-assistant-turn-interrupted">{t("assistantPage.turnInterrupted")}</span>
                  ) : (
                    <span className="mono">{msg.text}</span>
                  )}
                </Alert>
              </div>
            )}{turnLine(msg, i)}</Fragment>,
          )}
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

/** The `- ` error lines of a notice row (the backend's ``validation_codes.note_lines``). */
function noteLines(text: string): string[] {
  return text.split("\n").filter((l) => l.startsWith("- ")).map((l) => l.slice(2));
}

/** N17-obs468: the reply carried no proposal block although it claimed a change — the
 *  revision that is still current, or the one the server's single re-ask stored. */
function MissingNotice({ msg }: { msg: AssistantLiveMessage }) {
  const { t } = useTranslation();
  const state = msg.name === PROPOSAL_MISSING_PENDING ? "pending"
    : msg.name === PROPOSAL_MISSING_RECOVERED ? "recovered" : "missing";
  const base = msg.missing?.base ?? null;
  const key = state === "recovered" ? "recovered" : base === null ? `${state}NoBase` : state;
  return (
    <div data-testid="v2-assistant-proposal-missing" data-state={state}>
      <Alert tone={state === "recovered" ? "success" : state === "missing" ? "warn" : "info"}>
        {t(`assistantPage.proposalMissing.${key}`, { base, revision: msg.missing?.revision ?? "" })}
      </Alert>
    </div>
  );
}

/** N17-obs295: the turn's automatic proposal correction, with the first version's
 *  errors collapsed. */
function AutocorrectNotice({ msg }: { msg: AssistantLiveMessage }) {
  const { t } = useTranslation();
  const state = msg.name === PROPOSAL_AUTOCORRECT_PENDING ? "pending"
    : msg.name === PROPOSAL_AUTOCORRECT_FAILED ? "failed" : "done";
  const errors = errorDetails(noteLines(msg.text), msg.errors);
  return (
    <div data-testid="v2-assistant-autocorrect" data-state={state}>
      <Alert tone={state === "failed" ? "warn" : state === "done" ? "success" : "info"}>
        {t(`assistantPage.autocorrect.${state}`)}
        {errors.length > 0 && (
          <details style={{ marginTop: 6 }}>
            <summary className="v2-muted" style={{ fontSize: 12.5 }} data-testid="v2-assistant-autocorrect-errors-toggle">
              {t("assistantPage.autocorrect.errors", { count: errors.length })}
            </summary>
            <ValidationErrorList errors={errors} />
          </details>
        )}
      </Alert>
    </div>
  );
}

function CatalogSummary({ catalog, onRefresh }: { catalog: AssistantCatalog; onRefresh?: () => void }) {
  const { t } = useTranslation();
  const names = (items: { key?: string; kb_id?: string; name?: string }[]) =>
    items.length ? items.map((i) => i.name || i.key || i.kb_id).join(", ") : t("assistantPage.catalogNone");
  const res = catalog.resources;
  const memoryKind = catalogMemoryKind(res);
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
            value: names(catalog.knowledge_bases),
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
            value: memoryKind
              ? t("assistantPage.memoryWorkspaceAvailableKind", { kind: t(`assistantPage.memoryKind.${memoryKind}`) })
              : t("assistantPage.memoryNoShared"),
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
