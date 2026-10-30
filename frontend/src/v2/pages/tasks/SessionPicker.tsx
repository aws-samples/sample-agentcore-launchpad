import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type ObsSessionPreview, type ObsSessionRow } from "../../../lib/api";
import { fmtTime } from "../../format";
import { useLoad, usePaged } from "../../hooks";
import { Alert, Button, type Column, Descriptions, Drawer, FilterSelect, LinkButton, Pager, SearchInput, Spin, Table, Tag } from "../../ui";

const PAGE_SIZE = 10;

/**
 * Agent 轨迹 data source of the task wizard: the agent's observed sessions,
 * each row showing what was said — the first user input and the last agent
 * reply — so a session is picked by its content rather than its id. Previews
 * load one page per call (`/api/observability/session-previews`) and are kept
 * across page flips; 查看 opens the whole conversation. 全选 picks every row the
 * filters leave, the header checkbox the current page.
 */
export function SessionPicker({
  agentId,
  sessions,
  loading,
  error,
  onRefresh,
  selected,
  onChange,
}: {
  agentId: string;
  sessions: ObsSessionRow[];
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
  selected: string[];
  onChange: (sessionIds: string[]) => void;
}) {
  const { t } = useTranslation();
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [onlyPicked, setOnlyPicked] = useState(false);
  const [previews, setPreviews] = useState<Record<string, ObsSessionPreview>>({});
  const [open, setOpen] = useState<ObsSessionRow | null>(null);
  const pending = useRef(new Set<string>());

  const picked = useMemo(() => new Set(selected), [selected]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return sessions.filter((s) => {
      if (status === "ok" && s.errors) return false;
      if (status === "error" && !s.errors) return false;
      if (onlyPicked && !picked.has(s.session_id)) return false;
      return !needle || s.session_id.toLowerCase().includes(needle);
    });
  }, [sessions, q, status, onlyPicked, picked]);
  const paged = usePaged(rows, PAGE_SIZE);

  // one request for the page's rows that have no preview yet (and none in flight)
  useEffect(() => {
    const want = paged.slice.filter((s) => !previews[s.session_id] && !pending.current.has(s.session_id)).map((s) => s.session_id);
    if (want.length === 0) return;
    want.forEach((sid) => pending.current.add(sid));
    const merge = (got: ObsSessionPreview[]) =>
      setPreviews((prev) => ({ ...prev, ...Object.fromEntries(got.map((p) => [p.session_id, p])) }));
    const first = new Map(paged.slice.map((s) => [s.session_id, s.first]));
    api
      .obsSessionPreviews(
        want.map((sid) => ({ session_id: sid, first: first.get(sid) ?? null })),
        agentId,
      )
      .then((res) => merge(res.previews))
      .catch(() =>
        merge(want.map((sid) => ({ session_id: sid, available: false, reason: "read_failed", input: null, output: null, user_turns: 0 }))),
      )
      .finally(() => want.forEach((sid) => pending.current.delete(sid)));
  }, [paged.slice, previews, agentId]);

  const refresh = () => {
    setPreviews({});
    onRefresh();
  };
  const toggle = (sid: string) => onChange(picked.has(sid) ? selected.filter((s) => s !== sid) : [...selected, sid]);
  const pageIds = paged.slice.map((s) => s.session_id);
  const allOnPage = pageIds.length > 0 && pageIds.every((sid) => picked.has(sid));
  const togglePage = () =>
    onChange(allOnPage ? selected.filter((sid) => !pageIds.includes(sid)) : [...new Set([...selected, ...pageIds])]);
  const allRowsPicked = rows.length > 0 && rows.every((s) => picked.has(s.session_id));

  const columns: Column<ObsSessionRow>[] = [
    {
      key: "sel",
      title: (
        <input
          type="checkbox"
          aria-label={t("v2.traces.selectPage")}
          title={t("v2.traces.selectPage")}
          checked={allOnPage}
          disabled={pageIds.length === 0}
          onChange={togglePage}
          data-testid="v2-task-sessions-page"
        />
      ),
      width: 36,
      render: (s) => (
        <input
          type="checkbox"
          aria-label={s.session_id}
          checked={picked.has(s.session_id)}
          onChange={() => toggle(s.session_id)}
          data-testid={`v2-task-session-${s.session_id}`}
        />
      ),
    },
    {
      key: "sid",
      title: t("v2.traces.colSession"),
      render: (s) => {
        const turns = previews[s.session_id]?.user_turns;
        return (
          <>
            <span className="mono ellipsis" style={{ maxWidth: 150 }} title={s.session_id}>
              {s.session_id}
            </span>
            <span className="sub">
              {t("v2.tasks.sessions.traces", { count: s.traces })}
              {turns ? ` · ${t("v2.tasks.sessions.turns", { count: turns })}` : ""}
            </span>
          </>
        );
      },
    },
    { key: "input", title: t("v2.tasks.sessions.colInput"), render: (s) => <PreviewText preview={previews[s.session_id]} side="input" /> },
    { key: "output", title: t("v2.tasks.sessions.colOutput"), render: (s) => <PreviewText preview={previews[s.session_id]} side="output" /> },
    { key: "status", title: t("v2.traces.colStatus"), render: (s) => <SessionStatus session={s} /> },
    { key: "last", title: t("v2.pipelines.lastActive"), className: "nowrap", render: (s) => fmtTime(s.last) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (s) => (
        <LinkButton onClick={() => setOpen(s)} testId={`v2-task-session-view-${s.session_id}`}>
          {t("v2.common.view")}
        </LinkButton>
      ),
    },
  ];

  return (
    <>
      <div className="v2-toolbar">
        <FilterSelect
          label={t("v2.traces.colStatus")}
          value={status}
          allLabel={t("v2.common.all")}
          onChange={setStatus}
          options={[
            { value: "ok", label: t("v2.traces.ok") },
            { value: "error", label: t("v2.traces.errorShort") },
          ]}
        />
        <Button onClick={refresh}>{t("v2.common.refresh")}</Button>
        <Button
          disabled={rows.length === 0 || allRowsPicked}
          onClick={() => onChange([...new Set([...selected, ...rows.map((s) => s.session_id)])])}
          testId="v2-task-sessions-all"
        >
          {t("v2.tasks.sessions.selectAll", { count: rows.length })}
        </Button>
        <Button disabled={selected.length === 0} onClick={() => onChange([])} testId="v2-task-sessions-none">
          {t("v2.tasks.sessions.selectNone")}
        </Button>
        <label className="v2-check">
          <input type="checkbox" checked={onlyPicked} onChange={() => setOnlyPicked(!onlyPicked)} data-testid="v2-task-sessions-only" />
          {t("v2.tasks.sessions.onlyPicked")}
        </label>
        <div className="end">
          <SearchInput value={q} onChange={setQ} placeholder={t("v2.tasks.sessions.search")} testId="v2-task-sessions-q" />
          <span className="v2-count">{t("v2.tasks.sessions.picked", { count: selected.length })}</span>
        </div>
      </div>
      <p className="v2-muted" style={{ fontSize: 13, margin: "0 0 12px" }}>
        {t("v2.tasks.sessions.hint", { count: sessions.length })}
      </p>
      <Table
        columns={columns}
        rows={paged.slice}
        rowKey={(s) => s.session_id}
        loading={loading}
        error={error}
        onRetry={refresh}
        empty={sessions.length === 0 ? t("v2.tasks.previewNoSessions") : t("v2.tasks.sessions.emptyFiltered")}
        testId="v2-task-sessions-table"
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      {open && (
        <SessionDrawer
          session={open}
          agentId={agentId}
          picked={picked.has(open.session_id)}
          onToggle={() => toggle(open.session_id)}
          onClose={() => setOpen(null)}
        />
      )}
    </>
  );
}

function SessionStatus({ session }: { session: ObsSessionRow }) {
  const { t } = useTranslation();
  return session.errors ? <Tag tone="red">{t("v2.traces.error", { count: session.errors })}</Tag> : <Tag tone="green">{t("v2.traces.ok")}</Tag>;
}

function PreviewText({ preview, side }: { preview: ObsSessionPreview | undefined; side: "input" | "output" }) {
  const { t } = useTranslation();
  if (!preview) return <span className="v2-muted">…</span>;
  if (!preview.available) {
    // the reason is said once, in the input cell
    if (side === "output") return <span className="v2-muted">—</span>;
    const failed = preview.reason === "read_failed" || preview.reason === "memory_unavailable";
    return <span className="v2-muted">{t(failed ? "v2.tasks.sessions.readFailed" : "v2.tasks.sessions.noContent")}</span>;
  }
  const text = preview[side];
  if (!text) return <span className="v2-muted">{side === "output" ? t("v2.tasks.sessions.noReply") : "—"}</span>;
  return (
    <span className="clip" style={{ maxWidth: 340 }} title={text}>
      {text}
    </span>
  );
}

function SessionDrawer({
  session,
  agentId,
  picked,
  onToggle,
  onClose,
}: {
  session: ObsSessionRow;
  agentId: string;
  picked: boolean;
  onToggle: () => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const transcript = useLoad(
    () => api.obsSessionTranscript(session.session_id, agentId, session.first),
    `picker-transcript:${session.session_id}:${agentId}`,
  );
  const turns = transcript.data?.transcript.turns ?? [];
  return (
    <Drawer
      open
      title={t("v2.tasks.sessions.detailTitle")}
      onClose={onClose}
      testId="v2-task-session-drawer"
      footer={
        <Button kind={picked ? undefined : "primary"} onClick={onToggle} testId="v2-task-session-toggle">
          {picked ? t("v2.tasks.sessions.unpick") : t("v2.tasks.sessions.pick")}
        </Button>
      }
    >
      <div className="v2-stack" style={{ gap: 16 }}>
        <Descriptions
          one
          items={[
            { label: t("v2.traces.colSession"), value: <span className="mono">{session.session_id}</span> },
            { label: t("v2.pipelines.traceCount"), value: session.traces },
            { label: t("v2.traces.colStatus"), value: <SessionStatus session={session} /> },
            { label: t("v2.traces.colStart"), value: fmtTime(session.first) },
            { label: t("v2.pipelines.lastActive"), value: fmtTime(session.last) },
          ]}
        />
        <div>
          <h3 className="v2-sec-title" style={{ fontSize: 14 }}>
            {t("v2.traces.conversation")}
          </h3>
          {transcript.loading ? (
            <Spin />
          ) : transcript.error ? (
            <Alert tone="error">{transcript.error}</Alert>
          ) : turns.length === 0 ? (
            <p className="v2-muted">
              {transcript.data?.transcript.available === false ? t("v2.traces.noTranscript") : t("v2.traces.noTurns")}
            </p>
          ) : (
            turns.map((turn, i) => (
              <div key={i} className={turn.role.toLowerCase() === "user" ? "v2-turn user" : "v2-turn"}>
                <span className="who">{turn.role.toLowerCase() === "user" ? t("v2.traces.user") : "Agent"}</span>
                <div className="msg">{turn.text}</div>
              </div>
            ))
          )}
        </div>
      </div>
    </Drawer>
  );
}
