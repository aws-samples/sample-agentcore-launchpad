import type { RefObject } from "react";
import { useTranslation } from "react-i18next";

import { Markdown } from "../../../components";
import type { AssistantCatalog, AssistantConversationDetail } from "../../../lib/api";
import { type AssistantLiveMessage, stripProposalBlock } from "../../../lib/assistant";
import { Alert, Button, Descriptions, LinkButton, Tag } from "../../ui";
import { SECTION_IDS } from "./common";

export const COMPOSER_ID = "v2-assistant-input";

/** The streaming transcript, the composer and the workspace catalog summary. */
export function DiscussionCard({
  conversation, messages, input, onInput, busy, preparing, onSend, onRefreshCatalog, threadRef,
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
}) {
  const { t } = useTranslation();
  const marker = t("assistantPage.proposalInText");
  const sendDisabled = busy || preparing || !input.trim() || conversation.turn_in_progress !== null;
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
          {messages.map((msg, i) =>
            msg.role === "user" ? (
              <div key={i} className="v2-turn user">
                <div className="who">{t("assistantPage.you")}</div>
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
            ) : (
              <div key={i} data-testid="v2-assistant-turn-error">
                <Alert tone="error">
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
            ),
          )}
        </div>
        <div className="v2-assistant-composer">
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
          <div className="bar">
            <span className="v2-muted">{t("assistantPage.discussionNote")}</span>
            <Button kind="primary" disabled={sendDisabled} onClick={onSend} testId="v2-assistant-send">
              {busy ? t("assistantPage.sending") : t("assistantPage.send")}
            </Button>
          </div>
        </div>
        <CatalogSummary catalog={conversation.catalog} onRefresh={onRefreshCatalog} />
      </div>
    </section>
  );
}

function CatalogSummary({ catalog, onRefresh }: { catalog: AssistantCatalog; onRefresh: () => void }) {
  const { t } = useTranslation();
  const names = (items: { key?: string; kb_id?: string; name?: string }[]) =>
    items.length ? items.map((i) => i.name || i.key || i.kb_id).join(", ") : t("assistantPage.catalogNone");
  const res = catalog.resources;
  return (
    <div className="v2-assistant-section" data-testid="v2-assistant-catalog">
      <h3 className="v2-row">
        {t("assistantPage.catalogTitle")}
        <LinkButton onClick={onRefresh} testId="v2-assistant-catalog-refresh">{t("assistantPage.catalogRefresh")}</LinkButton>
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
