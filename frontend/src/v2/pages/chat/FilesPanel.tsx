import { Download } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { ApiError, chatApi, errorMessage, type SessionFile } from "../../../lib/api";
import { fileSize } from "../../../v3/pages/chat/inspect";
import { fmtTime } from "../../format";
import { Alert, Button, type Column, LinkButton, Spin, Table } from "../../ui";
import { FILES_IDLE, type FilesState } from "./filesState";

const NOT_OWNER_CODE = "chat.session_not_found";

/**
 * Files tab of the V2 chat inspector: what the agent saved to this session's output
 * folder, with a download per file. Reads when the session changes and when a turn ends
 * (`busy` falls) — the agent writes files during a turn. The session list shows every
 * session of the agent, but the files route is owner/admin only, so its 404 is the
 * "not yours" case, not an error.
 */
export function FilesPanel({
  agentId,
  sessionId,
  busy,
  editTo,
}: {
  agentId: string;
  sessionId: string | null;
  busy: boolean;
  editTo: string;
}) {
  const [read, setRead] = useState<{ key: string; state: FilesState }>({ key: "", state: FILES_IDLE });
  const seq = useRef(0);
  const key = sessionId ? `${agentId}:${sessionId}` : "";

  const load = async () => {
    if (!sessionId || !agentId) return;
    const mine = ++seq.current;
    setRead((r) => ({ key, state: { ...(r.key === key ? r.state : FILES_IDLE), loading: true, error: null } }));
    try {
      const data = await chatApi.files(agentId, sessionId);
      if (mine === seq.current) setRead({ key, state: { data, error: null, forbidden: false, loading: false } });
    } catch (err) {
      if (mine !== seq.current) return;
      const forbidden = err instanceof ApiError && err.code === NOT_OWNER_CODE;
      setRead({ key, state: { data: null, error: forbidden ? null : errorMessage(err), forbidden, loading: false } });
    }
  };

  useEffect(() => {
    if (key && !busy) void load();
    // `key` carries the agent and session; a turn ending re-reads
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, busy]);

  // another session's read is not this one's
  const state = read.key === key ? read.state : FILES_IDLE;
  return <FilesBody state={state} sessionId={sessionId} editTo={editTo} onRetry={() => void load()}
    onDownload={(path) => chatApi.fileUrl(agentId, sessionId ?? "", path)} />;
}

/** The tab's body from one read — the states, without the loading. */
export function FilesBody({
  state,
  sessionId,
  editTo,
  onRetry,
  onDownload,
}: {
  state: FilesState;
  sessionId: string | null;
  editTo: string;
  onRetry: () => void;
  onDownload: (path: string) => Promise<{ url: string }>;
}) {
  const { t } = useTranslation();
  const [downloading, setDownloading] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const { data } = state;

  if (!sessionId) return <div className="v2-chat-rail-empty" data-testid="files-placeholder">{t("v3.chat.files.placeholder")}</div>;
  if (state.forbidden) {
    return (
      <div data-testid="files-forbidden">
        <Alert tone="info">{t("v2.chat.filesForbidden")}</Alert>
      </div>
    );
  }
  if (!data) {
    if (state.error) {
      return (
        <div data-testid="files-error">
          <Alert tone="error" action={<LinkButton onClick={onRetry}>{t("v2.common.retry")}</LinkButton>}>
            {state.error}
          </Alert>
        </div>
      );
    }
    return <Spin />;
  }
  if (!data.enabled) {
    return (
      <div className="v2-stack" data-testid="files-disabled">
        <div className="v2-chat-rail-empty">{t("v3.chat.files.disabled")}</div>
        <Link className="v2-chat-extlink" to={editTo}>{t("v3.chat.files.enable")}</Link>
      </div>
    );
  }

  const download = async (path: string) => {
    setDownloading(path);
    setFailure(null);
    try {
      window.location.assign((await onDownload(path)).url);
    } catch (err) {
      setFailure(errorMessage(err));
    } finally {
      setDownloading(null);
    }
  };
  const columns: Column<SessionFile>[] = [
    { key: "name", title: t("v3.chat.files.colName"), render: (f) => <span className="mono" title={f.key_rel}>{f.key_rel}</span> },
    { key: "size", title: t("v3.chat.files.colSize"), render: (f) => fileSize(f.size) },
    { key: "time", title: t("v3.chat.files.colTime"), render: (f) => <small>{fmtTime(f.last_modified)}</small> },
    {
      key: "download",
      title: "",
      render: (f) => (
        <Button size="sm" disabled={downloading === f.key_rel} onClick={() => void download(f.key_rel)}
          title={`${t("v3.chat.files.download")} ${f.key_rel}`} testId="files-download">
          <Download size={13} aria-hidden="true" />
          {t("v3.chat.files.download")}
        </Button>
      ),
    },
  ];
  return (
    <div className="v2-stack" data-testid="files-panel">
      {failure && <Alert tone="error">{t("v3.chat.files.downloadFailed", { msg: failure })}</Alert>}
      <Table<SessionFile>
        columns={columns}
        rows={data.files}
        rowKey={(f) => f.key_rel}
        density="dense"
        empty={<span data-testid="files-empty">{t("v3.chat.files.empty", { path: data.mount_path ?? "/mnt/outputs" })}</span>}
        testId="files-table"
      />
      {data.truncated && <div className="v2-muted">{t("v3.chat.files.truncated", { count: data.files.length })}</div>}
      <div className="v2-muted">{t("v3.chat.files.retention", { days: data.retention_days })}</div>
    </div>
  );
}
