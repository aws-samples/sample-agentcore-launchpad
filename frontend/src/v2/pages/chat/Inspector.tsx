import { Copy, ExternalLink, KeyRound, Plus, RefreshCw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  type ApiKeyInfo,
  chatApi,
  type ChatMemorySummary,
  type ChatTraceInfo,
  errorMessage,
} from "../../../lib/api";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Confirm, Kpi, LinkButton, SubTabs, Tag } from "../../ui";
import { classicMemoryToV2 } from "../memory/classicUrl";
import { classicObservabilityToV2 } from "../observability/classicUrl";

export type InspectorTab = "trace" | "memory" | "api";

const SPAN_COLOR: Record<string, string> = {
  model: "#1664ff",
  tool: "#00b42a",
  memory: "#ff7d00",
  policy: "#722ed1",
  runtime: "#86909c",
  other: "#c9cdd4",
};

async function copy(text: string, toast: ReturnType<typeof useV2Toast>, done: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast("success", done);
  } catch (err) {
    toast("error", errorMessage(err));
  }
}

/** Right-hand debug rail: session trace, session memory, API equivalent + keys. */
export function Inspector({
  tab,
  onTab,
  agentId,
  sessionId,
  trace,
  traceBusy,
  onLoadTrace,
  memory,
}: {
  tab: InspectorTab;
  onTab: (tab: InspectorTab) => void;
  agentId: string;
  sessionId: string | null;
  trace: ChatTraceInfo | null;
  traceBusy: boolean;
  onLoadTrace: () => void;
  memory: ChatMemorySummary | null;
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
          <TracePanel sessionId={sessionId} trace={trace} busy={traceBusy} onLoad={onLoadTrace} />
        )}
        {tab === "memory" && <MemoryPanel sessionId={sessionId} memory={memory} />}
        {tab === "api" && <ApiPanel agentId={agentId} sessionId={sessionId} />}
      </div>
    </>
  );
}

function TracePanel({
  sessionId,
  trace,
  busy,
  onLoad,
}: {
  sessionId: string | null;
  trace: ChatTraceInfo | null;
  busy: boolean;
  onLoad: () => void;
}) {
  const { t } = useTranslation();
  const spans = trace?.spans.slice(0, 12) ?? [];
  const total = Math.max(...spans.map((s) => (s.start_ms ?? 0) + (s.duration_ms ?? 0)), 1);
  return (
    <div className="v2-stack" data-testid="trace-panel">
      <div className="v2-row">
        <Button size="sm" disabled={!sessionId || busy} onClick={onLoad} testId="trace-load">
          <RefreshCw size={13} aria-hidden="true" />
          {busy ? t("v2.common.loading") : t("v2.chat.traceLoad")}
        </Button>
        {sessionId && (
          <a
            className="v2-chat-extlink"
            href={classicObservabilityToV2(`?session=${encodeURIComponent(sessionId)}`)}
            target="_blank"
            rel="noreferrer"
            data-testid="open-in-obs"
          >
            {t("v2.chat.openInObs")}
            <ExternalLink size={12} aria-hidden="true" />
          </a>
        )}
        {trace?.cloudwatch_url && (
          <a className="v2-chat-extlink" href={trace.cloudwatch_url} target="_blank" rel="noreferrer">
            CloudWatch
            <ExternalLink size={12} aria-hidden="true" />
          </a>
        )}
      </div>
      {sessionId && <div className="v2-muted mono v2-chat-sid">session {sessionId}</div>}
      {trace && trace.span_count > 0 ? (
        <>
          <div data-testid="trace-rows">
            {spans.map((span, i) => (
              <div className="v2-span" key={i}>
                <span className="name" title={span.name}>
                  {span.name}
                </span>
                <span className="track">
                  <span
                    style={{
                      left: `${((span.start_ms ?? 0) / total) * 100}%`,
                      width: `${Math.max(((span.duration_ms ?? 0) / total) * 100, 0.8)}%`,
                      background: SPAN_COLOR[span.category] ?? SPAN_COLOR.other,
                    }}
                  />
                </span>
                <span className="dur">{Math.round(span.duration_ms ?? 0)}ms</span>
              </div>
            ))}
          </div>
          {trace.span_count > spans.length && (
            <div className="v2-muted">{t("v2.chat.traceMore", { count: trace.span_count - spans.length })}</div>
          )}
          <div className="v2-chat-legend">
            {(["model", "tool", "memory", "policy", "runtime"] as const).map((cat) => (
              <span key={cat}>
                <i style={{ background: SPAN_COLOR[cat] }} />
                {t(`v2.chat.spanCat.${cat}`)}
              </span>
            ))}
          </div>
        </>
      ) : (
        <div className="v2-chat-rail-empty">
          {sessionId ? (trace ? t("v2.chat.traceEmpty") : t("v2.chat.traceIdle")) : t("v2.chat.tracePlaceholder")}
        </div>
      )}
    </div>
  );
}

function MemoryPanel({ sessionId, memory }: { sessionId: string | null; memory: ChatMemorySummary | null }) {
  const { t } = useTranslation();
  return (
    <div className="v2-stack" data-testid="memory-panel">
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

function ApiPanel({ agentId, sessionId }: { agentId: string; sessionId: string | null }) {
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
      setNewKey(await chatApi.createApiKey(`console-${rows.length + 1}`));
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
          </span>
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
