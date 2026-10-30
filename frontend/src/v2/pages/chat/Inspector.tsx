import { copyBrowserText } from "../../../lib/browser-compat";
import { Copy, ExternalLink, KeyRound, Plus } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  type ApiKeyInfo,
  chatApi,
  type ChatMemorySummary,
  type ChatTraceInfo,
  errorMessage,
} from "../../../lib/api";
import type { AgentMemoryState } from "../../../lib/chat";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Confirm, Kpi, LinkButton, SubTabs, Tag } from "../../ui";
import { classicMemoryToV2 } from "../memory/classicUrl";
import { apiKeyMeta, apiKeyOffboardedNote } from "./apiKeys";
import { TracePanel } from "./TracePanel";

export type InspectorTab = "trace" | "memory" | "api";

async function copy(text: string, toast: ReturnType<typeof useV2Toast>, done: string) {
  try {
    if (await copyBrowserText(text)) toast("success", done);
  } catch (err) {
    toast("error", errorMessage(err));
  }
}

/** Right-hand debug rail: session trace, session memory, API equivalent + keys. */
export function Inspector({
  tab,
  onTab,
  agentId,
  agentName,
  sessionId,
  memory,
  memoryState,
  trace,
  traceBusy,
  traceError,
  onLoadTrace,
}: {
  tab: InspectorTab;
  onTab: (tab: InspectorTab) => void;
  agentId: string;
  /** Recorded as the new key's label (N-SCN-obs4) — keys stay workspace-wide. */
  agentName?: string;
  sessionId: string | null;
  memory: ChatMemorySummary | null;
  memoryState: AgentMemoryState;
  /** `GET /api/traces/{session_id}` for the current session, null until loaded. */
  trace: ChatTraceInfo | null;
  traceBusy: boolean;
  traceError?: string | null;
  onLoadTrace: () => void;
}) {
  const { t } = useTranslation();
  return (
    <>
      <div className="v2-chat-inspector-tabs">
        <SubTabs
          value={tab}
          onChange={onTab}
          tabs={[
            { value: "trace", label: t("v2.chat.tabTrace") },
            { value: "memory", label: t("v2.chat.tabMemory") },
            { value: "api", label: t("v2.chat.tabApi") },
          ]}
        />
      </div>
      <div className="v2-chat-inspector-body">
        {tab === "trace" && (
          <TracePanel sessionId={sessionId} trace={trace} busy={traceBusy} error={traceError} onLoad={onLoadTrace} />
        )}
        {tab === "memory" && <MemoryPanel sessionId={sessionId} memory={memory} memoryState={memoryState} />}
        {tab === "api" && <ApiPanel agentId={agentId} agentName={agentName} sessionId={sessionId} />}
      </div>
    </>
  );
}

function MemoryPanel({
  sessionId,
  memory,
  memoryState,
}: {
  sessionId: string | null;
  memory: ChatMemorySummary | null;
  memoryState: AgentMemoryState;
}) {
  const { t } = useTranslation();
  return (
    <div className="v2-stack" data-testid="memory-panel">
      {memoryState === "off" && (
        <div className="v2-chat-rail-empty" data-testid="memory-disabled-note">
          {t("v2.chat.memoryDisabledNote")}
        </div>
      )}
      <div className="v2-chat-kpis">
        <Kpi label={t("chatPage.shortTermEvents")} value={memory?.event_count ?? 0} />
        <Kpi label={t("chatPage.longTermRecords")} value={memory?.records.length ?? 0} />
      </div>
      {sessionId && memory?.actor_id && (
        <a
          className="v2-chat-extlink"
          href={classicMemoryToV2(
            `?view=short-term&actor=${encodeURIComponent(memory.actor_id)}&session=${encodeURIComponent(sessionId)}`,
          )}
          target="_blank"
          rel="noreferrer"
          data-testid="open-in-memory"
        >
          {t("v2.chat.openInMemory")}
          <ExternalLink size={12} aria-hidden="true" />
        </a>
      )}
      {!sessionId && <div className="v2-chat-rail-empty">{t("v2.chat.memoryPlaceholder")}</div>}
      {memory && memory.records.length > 0 && (
        <div className="v2-chat-records">
          {memory.records.map((r, i) => (
            <div key={i} className="v2-chat-record">
              <span className="v2-chat-record-ns mono">{r.namespace}</span>
              <span>{r.text}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ApiPanel({ agentId, agentName, sessionId }: { agentId: string; agentName?: string; sessionId: string | null }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const keys = useLoad(() => chatApi.apiKeys(), "apikeys");
  const [newKey, setNewKey] = useState<ApiKeyInfo | null>(null);
  const [creating, setCreating] = useState(false);
  const [toggling, setToggling] = useState<string | null>(null);
  const [confirmDisable, setConfirmDisable] = useState<ApiKeyInfo | null>(null);
  const rows = keys.data?.keys ?? [];

  const snippet = `curl -N -X POST \\
  ${window.location.origin}/v1/agents/${agentId || "<id>"}/invoke-stream \\
  -H "x-api-key: lp_live_…" \\
  -d '{"prompt":"…","session_id":${sessionId ? `"${sessionId}"` : "null"}}'`;

  const create = async () => {
    setCreating(true);
    try {
      // The label records which agent's page minted the key (workspace-wide
      // keys outlive agents, N-SCN-obs4) — display only, `/v1` ignores it.
      setNewKey(await chatApi.createApiKey(`console-${rows.length + 1}`, agentName));
      keys.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setCreating(false);
    }
  };

  const toggle = async (key: ApiKeyInfo) => {
    setToggling(key.id);
    try {
      await chatApi.setApiKeyEnabled(key.id, !key.enabled);
      keys.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setToggling(null);
    }
  };

  return (
    <div className="v2-stack" data-testid="api-panel">
      <div className="v2-sub-title v2-chat-sub">
        {t("v2.chat.apiTitle")}
        <LinkButton onClick={() => void copy(snippet, toast, t("v2.chat.copied"))}>
          <Copy size={12} aria-hidden="true" /> {t("v2.chat.copy")}
        </LinkButton>
      </div>
      <div className="v2-muted">{t("v2.chat.apiHint")}</div>
      <pre className="v2-pre v2-chat-snippet">{snippet}</pre>

      <div className="v2-sub-title v2-chat-sub">
        {t("v2.chat.keysTitle")}
        <Button size="sm" disabled={creating} onClick={() => void create()} testId="new-key">
          <Plus size={13} aria-hidden="true" />
          {t("v2.chat.newKey")}
        </Button>
      </div>
      {newKey?.key && (
        <Alert
          tone="success"
          action={
            <LinkButton onClick={() => void copy(newKey.key ?? "", toast, t("v2.chat.copied"))}>
              {t("v2.chat.copy")}
            </LinkButton>
          }
        >
          <div>{t("chatPage.keyOnce")}</div>
          <code className="v2-chat-newkey" data-testid="new-key-value">
            {newKey.key}
          </code>
        </Alert>
      )}
      {keys.error && (
        <Alert tone="error" action={<LinkButton onClick={keys.reload}>{t("v2.common.retry")}</LinkButton>}>
          {keys.error}
        </Alert>
      )}
      {!keys.error && !keys.loading && rows.length === 0 && (
        <div className="v2-chat-rail-empty">{t("v2.chat.noKeys")}</div>
      )}
      {rows.map((key) => (
        <div className="v2-chat-key" key={key.id} data-testid="api-key-row">
          <KeyRound size={14} aria-hidden="true" />
          <span className="v2-chat-key-name">
            <span>{key.name}</span>
            <span className="mono v2-muted">{key.prefix}</span>
            <span className="v2-muted" data-testid="api-key-meta">
              {apiKeyMeta(t, key)}
            </span>
          </span>
          {key.label && (
            <Tag tone="outline" title={t("v2.chat.keyLabelHint", { label: key.label })} testId="api-key-label">
              {key.label}
            </Tag>
          )}
          {key.creator_offboarded && (
            <Tag tone="orange" title={apiKeyOffboardedNote(t, key) ?? undefined}>
              {t("v2.chat.keyCreatorOffboarded")}
            </Tag>
          )}
          <Tag tone={key.enabled ? "green" : "gray"}>
            {key.enabled ? t("v2.chat.keyEnabled") : t("v2.chat.keyDisabled")}
          </Tag>
          <LinkButton
            danger={key.enabled}
            disabled={toggling === key.id}
            onClick={() => (key.enabled ? setConfirmDisable(key) : void toggle(key))}
          >
            {key.enabled ? t("v2.chat.disable") : t("v2.chat.enable")}
          </LinkButton>
        </div>
      ))}
      <Confirm
        open={confirmDisable !== null}
        title={t("chatPage.confirmDisableKey.title")}
        body={t("chatPage.confirmDisableKey.body", { name: confirmDisable?.name ?? "" })}
        confirmLabel={t("v2.chat.disable")}
        danger
        onConfirm={() => {
          if (confirmDisable) void toggle(confirmDisable);
          setConfirmDisable(null);
        }}
        onClose={() => setConfirmDisable(null)}
      />
    </div>
  );
}
