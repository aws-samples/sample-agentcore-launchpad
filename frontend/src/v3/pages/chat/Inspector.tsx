import { Download, ExternalLink, RefreshCw, X } from "lucide-react";
import { type Dispatch, type MutableRefObject, type SetStateAction, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { chatApi, type ChatMemorySummary, type ChatTraceInfo, errorMessage, type SessionFiles } from "../../../lib/api";
import { MemoryRecordText } from "../../../components/MemoryRecordText";
import type { AgentMemoryState } from "../../../lib/chat";
import { Btn, Filters, Notice, Skeleton } from "../../ui";
import { when } from "../agent/format";
import { fileSize, type InspectTab, spanBars } from "./inspect";

const CATEGORIES = ["model", "tool", "memory", "policy", "runtime"] as const;

/** What one tab read: its data, or why it could not be read. */
interface Read<T> {
  data: T | null;
  error: string | null;
  busy: boolean;
}

const idle = <T,>(): Read<T> => ({ data: null, error: null, busy: false });

/**
 * The chat's right rail (V2's Inspector, rebuilt): the session's trace waterfall, its
 * memory and the files the agent saved to its session output folder. The open tab loads when the rail opens, when the session changes and
 * once after every finished turn (`busy` falling) — never while a turn runs. The
 * parent keys it on agent + session, so another conversation starts empty.
 */
export function Inspector({
  tab,
  onTab,
  onClose,
  agentId,
  editTo,
  sessionId,
  busy,
  memoryState,
}: {
  tab: InspectTab;
  onTab: (tab: InspectTab) => void;
  onClose: () => void;
  agentId: string;
  /** where the agent's settings open (`editPath`), for the disabled Files tab */
  editTo: string;
  sessionId: string | undefined;
  busy: boolean;
  memoryState: AgentMemoryState;
}) {
  const { t } = useTranslation();
  const [trace, setTrace] = useState<Read<ChatTraceInfo>>(idle);
  const [memory, setMemory] = useState<Read<ChatMemorySummary>>(idle);
  const [files, setFiles] = useState<Read<SessionFiles>>(idle);
  const traceSeq = useRef(0);
  const memorySeq = useRef(0);
  const filesSeq = useRef(0);

  // one read per tab; a newer read of the same tab supersedes an older one
  const read = async <T,>(set: Dispatch<SetStateAction<Read<T>>>, seq: MutableRefObject<number>, fetch: () => Promise<T>) => {
    const mine = ++seq.current;
    set((r) => ({ ...r, busy: true, error: null }));
    try {
      const data = await fetch();
      if (mine === seq.current) set({ data, error: null, busy: false });
    } catch (err) {
      if (mine === seq.current) set((r) => ({ ...r, error: errorMessage(err), busy: false }));
    }
  };

  const load = (which: InspectTab) => {
    if (!sessionId) return;
    if (which === "trace") void read(setTrace, traceSeq, () => chatApi.trace(sessionId));
    else if (which === "files") void read(setFiles, filesSeq, () => chatApi.files(agentId, sessionId));
    else if (memoryState !== "off") void read(setMemory, memorySeq, () => chatApi.memory(agentId, sessionId));
  };

  useEffect(() => {
    if (!busy) load(tab);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, busy, sessionId]);

  const active = tab === "trace" ? trace : tab === "files" ? files : memory;
  return (
    <aside className="v3-chat-inspect" data-testid="v3-chat-inspector" aria-label={t("v3.chat.inspect.title")}>
      <header className="v3-chat-inspect-head">
        <Filters
          value={tab}
          onChange={onTab}
          options={[
            { value: "trace", label: t("v2.chat.tabTrace") },
            { value: "memory", label: t("v2.chat.tabMemory") },
            { value: "files", label: t("v3.chat.inspect.tabFiles") },
          ]}
        />
        <button type="button" className="v3-btn ghost sm" onClick={onClose} aria-label={t("v3.chat.inspect.close")}
          title={t("v3.chat.inspect.close")} data-testid="v3-chat-inspect-close">
          <X size={14} />
        </button>
      </header>
      <div className="v3-chat-inspect-body">
        {!sessionId ? (
          <p className="v3-chat-inspect-empty">
            {tab === "trace" ? t("v2.chat.tracePlaceholder") : tab === "files" ? t("v3.chat.files.placeholder") : t("v2.chat.memoryPlaceholder")}
          </p>
        ) : (
          <>
            <div className="v3-chat-inspect-tools">
              {!(tab === "memory" && memoryState === "off") && (
                <Btn size="sm" busy={active.busy || busy} onClick={() => load(tab)} data-testid="v3-chat-inspect-load">
                  <RefreshCw size={13} /> {tab === "trace" ? t("v2.chat.traceLoad") : t("v3.chat.inspect.refresh")}
                </Btn>
              )}
              {tab === "trace" ? (
                <>
                  <Link className="v3-chat-inspect-link" target="_blank" rel="noreferrer" data-testid="v3-chat-open-obs"
                    to={`/v3/observability?tab=sessions&view=session&id=${encodeURIComponent(sessionId)}`}>
                    {t("v2.chat.openInObs")} <ExternalLink size={12} />
                  </Link>
                  {trace.data?.cloudwatch_url && (
                    <a className="v3-chat-inspect-link" href={trace.data.cloudwatch_url} target="_blank" rel="noreferrer">
                      CloudWatch <ExternalLink size={12} />
                    </a>
                  )}
                </>
              ) : tab === "files" ? null : (
                memory.data?.actor_id && (
                  <Link className="v3-chat-inspect-link" target="_blank" rel="noreferrer" data-testid="v3-chat-open-memory"
                    to={`/v3/memory?tab=short-term&actor=${encodeURIComponent(memory.data.actor_id)}&session=${encodeURIComponent(sessionId)}`}>
                    {t("v2.chat.openInMemory")} <ExternalLink size={12} />
                  </Link>
                )
              )}
            </div>
            <div className="v3-chat-inspect-sid mono" title={sessionId}>session {sessionId}</div>
            {active.error && <Notice s="act">{t("v3.chat.inspect.failed", { msg: active.error })}</Notice>}
            {tab === "trace" ? <TraceBody read={trace} /> : tab === "files" ? (
              <FilesBody read={files} agentId={agentId} editTo={editTo} sessionId={sessionId} />
            ) : <MemoryBody read={memory} memoryState={memoryState} />}
          </>
        )}
      </div>
    </aside>
  );
}

function TraceBody({ read }: { read: Read<ChatTraceInfo> }) {
  const { t } = useTranslation();
  const trace = read.data;
  if (!trace) return read.busy ? <Skeleton rows={4} /> : read.error ? null : <p className="v3-chat-inspect-empty">{t("v2.chat.traceIdle")}</p>;
  if (trace.span_count === 0) return <p className="v3-chat-inspect-empty" data-testid="v3-chat-trace-empty">{t("v2.chat.traceEmpty")}</p>;
  const bars = spanBars(trace);
  return (
    <div className="v3-chat-trace" data-testid="v3-chat-trace">
      <div className="v3-chat-spans">
        {bars.map(({ span, left, width }, i) => (
          <div className="v3-chat-span" key={i} data-testid="v3-chat-span">
            <span className="name" title={span.name}>{span.name}</span>
            <span className="track">
              <span data-cat={span.category} style={{ left: `${left}%`, width: `${width}%` }} />
            </span>
            <span className="dur mono">{Math.round(span.duration_ms ?? 0)}ms</span>
          </div>
        ))}
      </div>
      {trace.span_count > bars.length && (
        <p className="v3-chat-inspect-empty">{t("v2.chat.traceMore", { count: trace.span_count - bars.length })}</p>
      )}
      <div className="v3-chat-legend">
        {CATEGORIES.map((cat) => (
          <span key={cat}><i data-cat={cat} />{t(`v2.chat.spanCat.${cat}`)}</span>
        ))}
      </div>
    </div>
  );
}

function MemoryBody({ read, memoryState }: { read: Read<ChatMemorySummary>; memoryState: AgentMemoryState }) {
  const { t } = useTranslation();
  if (memoryState === "off") {
    return <p className="v3-chat-inspect-empty" data-testid="v3-chat-memory-off">{t("v2.chat.memoryDisabledNote")}</p>;
  }
  const memory = read.data;
  if (!memory) return read.busy ? <Skeleton rows={3} /> : null;
  return (
    <div className="v3-chat-memory" data-testid="v3-chat-memory">
      <div className="v3-chat-memory-kpis">
        <div className="v3-stat"><span className="label">{t("chatPage.shortTermEvents")}</span><span className="value">{memory.event_count}</span></div>
        <div className="v3-stat"><span className="label">{t("chatPage.longTermRecords")}</span><span className="value">{memory.records.length}</span></div>
      </div>
      {memory.records.map((r, i) => (
        <div key={i} className="v3-chat-record">
          <span className="ns mono">{r.namespace}</span>
          <MemoryRecordText text={r.text} />
        </div>
      ))}
    </div>
  );
}

/** The session output folder: what the agent saved, newest first, with a download per
 *  file (a presigned URL fetched on click — it lives five minutes). */
export function FilesBody({ read, agentId, editTo, sessionId }: { read: Read<SessionFiles>; agentId: string; editTo: string; sessionId: string }) {
  const { t } = useTranslation();
  const [downloading, setDownloading] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const data = read.data;
  if (!data) return read.busy ? <Skeleton rows={3} /> : null;
  if (!data.enabled) {
    return (
      <div className="v3-chat-inspect-empty" data-testid="v3-chat-files-disabled">
        <p>{t("v3.chat.files.disabled")}</p>
        <Link className="v3-chat-inspect-link" to={editTo}>
          {t("v3.chat.files.enable")}
        </Link>
      </div>
    );
  }
  const download = async (path: string) => {
    setDownloading(path);
    setFailure(null);
    try {
      const { url } = await chatApi.fileUrl(agentId, sessionId, path);
      window.location.assign(url);
    } catch (err) {
      setFailure(errorMessage(err));
    } finally {
      setDownloading(null);
    }
  };
  return (
    <div className="v3-chat-files" data-testid="v3-chat-files">
      {failure && <Notice s="act">{t("v3.chat.files.downloadFailed", { msg: failure })}</Notice>}
      {data.files.length === 0 ? (
        <p className="v3-chat-inspect-empty" data-testid="v3-chat-files-empty">
          {t("v3.chat.files.empty", { path: data.mount_path ?? "/mnt/outputs" })}
        </p>
      ) : (
        <table className="v3-table">
          <thead>
            <tr>
              <th>{t("v3.chat.files.colName")}</th>
              <th>{t("v3.chat.files.colSize")}</th>
              <th>{t("v3.chat.files.colTime")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.files.map((f) => (
              <tr key={f.key_rel} data-testid="v3-chat-file">
                <td className="mono" title={f.key_rel}>{f.key_rel}</td>
                <td>{fileSize(f.size)}</td>
                <td><small>{when(f.last_modified)}</small></td>
                <td>
                  <Btn size="sm" busy={downloading === f.key_rel} onClick={() => void download(f.key_rel)}
                    aria-label={`${t("v3.chat.files.download")} ${f.key_rel}`} data-testid="v3-chat-file-download">
                    <Download size={13} /> {t("v3.chat.files.download")}
                  </Btn>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {data.truncated && <small className="v3-hint">{t("v3.chat.files.truncated", { count: data.files.length })}</small>}
      <small className="v3-hint">{t("v3.chat.files.retention", { days: data.retention_days })}</small>
    </div>
  );
}
