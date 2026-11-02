import { ArrowLeft, FileText, Pencil, Plus, RefreshCw, Search, Trash2, Upload } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { ApiErrorDetail } from "../../components/ApiErrorDetail";
import { ErrorCallout } from "../../components/ErrorCallout";
import { api, ApiError, errorMessage, v2KnowledgeApi } from "../../lib/api";
import {
  type DataSource,
  extractConflictAgents,
  formatBytes,
  humanizeStat,
  jobRunning,
  KB_DESCRIPTION_MAX,
  KB_QUERY_MAX_RESULTS,
  kbInFlight,
  type KBDocument,
  type KnowledgeBaseDetail,
  type KnowledgeBaseSummary,
  mergeFiles,
  type QueryResultItem,
  scoresUninformative,
  sourceReady,
} from "../../lib/knowledgeBases";
import { useWorkspace } from "../../workspace/workspace-context";
import { useFailure } from "../failure";
import { ago } from "../format";
import { useLoad, useToast } from "../hooks";
import { Reasoned } from "../reason";
import { SharedFromTag } from "../shares/SharedFromTag";
import { kbSignal, resourceSignal } from "../signals";
import {
  Btn,
  Chip,
  Confirm,
  Dialog,
  Empty,
  Filters,
  Lamp,
  Notice,
  PageHead,
  Panel,
  type Signal,
  Skeleton,
  Stat,
  Track,
  type TrackNode,
} from "../ui";
import { Term } from "../onboarding/Glossary";
import { Select } from "../select";
import { kbDeleteBlockedReason } from "../../v2/pages/knowledge/status";

/* Same cadence and guards as V2's detail page, whose create-flow automation this
   page now carries (a KB created in the hosted V2 form lands here). */
const POLL_MS = 5000;
// how long a slow create may take to get its data source from the backend thread
const SOURCE_WAIT_TICKS = 40;
const GONE_CODES = new Set(["kb.not_found", "aws.not_found", "aws.validation", "aws.access_denied", "http.404"]);
const DOC_PAGE = 50;

function useKbStatus() {
  const { t } = useTranslation();
  return (status: string) => t(`knowledge.status.${String(status).toLowerCase()}`, { defaultValue: status });
}

/** Delete a KB: confirm → DELETE; a 409 naming mounted agents asks again with force. */
function useKbDelete(onDeleted: () => void) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const [target, setTarget] = useState<{ kb_id: string; name: string } | null>(null);
  const [conflict, setConflict] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const close = () => {
    setTarget(null);
    setConflict(null);
  };
  const run = async (force: boolean) => {
    if (!target) return;
    setBusy(true);
    try {
      failed.clear();
      await v2KnowledgeApi.remove(target.kb_id, force);
      toast("ok", t("knowledge.detail.deleted", { name: target.name }));
      close();
      onDeleted();
    } catch (err) {
      if (err instanceof ApiError && err.code === "kb.has_attached_agents" && !force) {
        setConflict(extractConflictAgents({ detail: err.detail }));
      } else {
        failed.fail(err, t("common.actionFailed", { msg: errorMessage(err) }));
        close();
      }
    } finally {
      setBusy(false);
    }
  };
  const dialog = target ? (
    <Confirm
      title={conflict ? t("knowledge.detail.delete.conflictTitle") : t("knowledge.detail.delete.title")}
      confirmLabel={conflict ? t("knowledge.detail.delete.force") : t("v3.kb.delete")}
      cancelLabel={t("v3.common.cancel")}
      danger
      busy={busy}
      onCancel={close}
      onConfirm={() => void run(conflict !== null)}
    >
      {conflict
        ? t("knowledge.detail.delete.conflictBody", { agents: conflict.join(", ") || "—" })
        : t("knowledge.detail.delete.body", { name: target.name })}
    </Confirm>
  ) : null;
  return { ask: (kb: { kb_id: string; name: string }) => setTarget(kb), dialog, failed };
}

/* ── shelf ───────────────────────────────────────────────────────────────── */

function KbShelf() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { current } = useWorkspace();
  const statusLabel = useKbStatus();
  const list = useLoad(() => v2KnowledgeApi.list(), `v3-kbs:${current?.id ?? ""}`);
  const [state, setState] = useState<"all" | Signal>("all");
  const [q, setQ] = useState("");
  const kbs = useMemo(() => list.data?.items ?? [], [list.data]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return kbs
      .filter((kb) => state === "all" || kbSignal(kb.status) === state)
      .filter((kb) => !needle || `${kb.name} ${kb.kb_id} ${kb.description}`.toLowerCase().includes(needle));
  }, [kbs, state, q]);
  const count = (s: Signal) => kbs.filter((kb) => kbSignal(kb.status) === s).length;
  const agents = new Set(kbs.flatMap((kb) => kb.attached_agents)).size;
  // a KB provisioning or deleting moves on its own: refresh until it settles
  const reload = list.reload;
  const transient = kbs.some((kb) => kbSignal(kb.status) === "wait");
  useEffect(() => {
    if (!transient) return;
    const timer = window.setInterval(reload, 8000);
    return () => window.clearInterval(timer);
  }, [transient, reload]);

  const open = (kb: KnowledgeBaseSummary) => navigate(`/v3/knowledge?id=${encodeURIComponent(kb.kb_id)}`);

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.kb.eyebrow")}
        title={t("v3.kb.title")}
        sub={t("v3.kb.sub")}
        end={<Link to="/v2/knowledge-bases?view=new" className="v3-btn primary"><Plus size={14} /> {t("v3.kb.create")}</Link>}
      />
      <div className="v3-grid c4">
        <Panel><Stat label={<Term term="knowledgeBase">{t("v3.kb.total")}</Term>} value={list.data ? kbs.length : "—"} /></Panel>
        <Panel><Stat label={t("v3.kb.ready")} value={list.data ? count("ok") : "—"} signal={count("ok") ? "ok" : undefined} /></Panel>
        <Panel signal={count("wait") ? "wait" : undefined}>
          <Stat label={t("v3.kb.moving")} value={list.data ? count("wait") : "—"} foot={t("v3.kb.movingFoot")} />
        </Panel>
        <Panel signal={count("act") ? "act" : undefined}>
          <Stat label={t("v3.kb.mounted")} value={list.data ? agents : "—"}
            foot={count("act") ? t("v3.kb.failedFoot", { count: count("act") }) : t("v3.kb.mountedFoot")} />
        </Panel>
      </div>

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters
          value={state}
          onChange={setState}
          options={[
            { value: "all", label: t("v3.kb.all"), count: kbs.length },
            { value: "ok", label: statusLabel("ACTIVE"), s: "ok", count: count("ok") },
            { value: "wait", label: t("v3.kb.inFlight"), s: "wait", count: count("wait") },
            { value: "act", label: statusLabel("FAILED"), s: "act", count: count("act") },
          ]}
        />
        <div style={{ marginLeft: "auto", position: "relative", flex: "1 1 200px", maxWidth: 300 }}>
          <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
          <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => setQ(e.target.value)}
            placeholder={t("v3.kb.search")} aria-label={t("v3.kb.search")} />
        </div>
      </div>

      {list.loading && !list.data ? (
        <Panel><Skeleton rows={5} /></Panel>
      ) : list.error ? (
        <Notice s="act"><ApiErrorDetail error={list.failure ?? list.error} /></Notice>
      ) : rows.length === 0 ? (
        <Panel><Empty title={kbs.length ? t("v3.kb.none") : t("v3.kb.empty")}>{!kbs.length && t("v3.kb.emptySub")}</Empty></Panel>
      ) : (
        <div className="v3-shelf">
          {rows.map((kb) => {
            const s = kbSignal(kb.status);
            return (
              <button key={kb.kb_id} type="button" className="v3-panel v3-kb" data-signal={s === "off" ? undefined : s} onClick={() => open(kb)}>
                <div className="top">
                  <Lamp s={s} live={s === "ok" || s === "wait"} />
                  <b>{kb.name}</b>
                  <SharedFromTag row={kb} />
                  <span style={{ marginLeft: "auto" }}><Chip s={s === "off" ? undefined : s}>{statusLabel(kb.status)}</Chip></span>
                </div>
                <p>{kb.description || t("v3.kb.noDescription")}</p>
                <div className="meta">
                  <span><b>{kb.data_source_count}</b> {t("v3.kb.sources", { count: kb.data_source_count })}</span>
                  <span><b>{kb.attached_agents.length}</b> {t("v3.kb.agents", { count: kb.attached_agents.length })}</span>
                  <span style={{ marginLeft: "auto" }}>{ago(kb.updated_at)}</span>
                </div>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* ── one knowledge base ──────────────────────────────────────────────────── */

function Documents({ kbId, ds }: { kbId: string; ds: DataSource }) {
  const { t } = useTranslation();
  const [docs, setDocs] = useState<KBDocument[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notReady, setNotReady] = useState<string | null>(null);
  const ready = sourceReady(ds.status);
  const load = useCallback(
    async (token: string | null) => {
      setLoading(true);
      setError(null);
      try {
        const page = await v2KnowledgeApi.documents(kbId, ds.ds_id, DOC_PAGE, token);
        if (page.ready === false) {
          setNotReady(page.data_source_status ?? ds.status);
          setDocs([]);
          setNext(null);
          return;
        }
        setNotReady(null);
        setDocs((prev) => (token ? [...(prev ?? []), ...page.documents] : page.documents));
        setNext(page.next_token ?? null);
      } catch (err) {
        setError(err);
      } finally {
        setLoading(false);
      }
    },
    [kbId, ds.ds_id, ds.status],
  );
  useEffect(() => {
    // a data source that is not AVAILABLE yet has no document list to ask for
    if (ready) void load(null);
  }, [load, ready]);

  return (
    <Panel title={t("v3.kb.documentsOf", { name: ds.name })} flush end={docs ? <span className="mono">{docs.length}{next ? "+" : ""}</span> : undefined}>
      {!ready || notReady ? (
        <div style={{ padding: 20 }} data-testid="v3-kb-docs-not-ready">
          <Notice s="wait">{t("v2.knowledge.docsNotReady", { status: notReady ?? ds.status ?? "—" })}</Notice>
        </div>
      ) : error != null ? (
        <div style={{ padding: 20 }}><Notice s="act"><ApiErrorDetail error={error} /></Notice></div>
      ) : !docs ? (
        <div style={{ padding: 20 }}><Skeleton rows={3} /></div>
      ) : docs.length === 0 ? (
        <Empty title={t("v3.kb.noDocuments")} />
      ) : (
        <>
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("knowledge.detail.sources.docName")}</th>
                <th className="num">{t("knowledge.detail.sources.docSize")}</th>
                <th className="num">{t("knowledge.detail.sources.docUploaded")}</th>
                <th>{t("knowledge.detail.sources.docStatus")}</th>
              </tr>
            </thead>
            <tbody>
              {docs.map((d) => (
                <tr key={d.uri}>
                  <td style={{ width: 30 }}><Lamp s={resourceSignal(d.status)} /></td>
                  <td title={d.uri}><span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}><FileText size={14} style={{ color: "var(--v3-text-3)" }} />{d.name}</span></td>
                  <td className="num">{formatBytes(d.size_bytes)}</td>
                  <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(d.uploaded_at)}</td>
                  <td><Chip s={resourceSignal(d.status) === "off" ? undefined : resourceSignal(d.status)} title={d.status_reason ?? undefined}>{d.status}</Chip></td>
                </tr>
              ))}
            </tbody>
          </table>
          {next && (
            <div style={{ padding: 14, display: "flex", justifyContent: "center" }}>
              <Btn size="sm" disabled={loading} onClick={() => void load(next)}>{t("v3.kb.loadMore")}</Btn>
            </div>
          )}
        </>
      )}
    </Panel>
  );
}

function Retrieve({ kb }: { kb: KnowledgeBaseDetail }) {
  const { t } = useTranslation();
  const [q, setQ] = useState("");
  const [n, setN] = useState(5);
  const [hits, setHits] = useState<QueryResultItem[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const ready = String(kb.status).toUpperCase() === "ACTIVE";
  const run = async () => {
    if (!q.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setHits((await v2KnowledgeApi.query(kb.kb_id, q.trim(), n)).results);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const top = Math.max(...(hits ?? []).map((h) => h.score ?? 0), 0) || 1;
  // every result scored exactly 1.0 = a lone indexed chunk, not a relevance verdict
  const showScores = !(hits && scoresUninformative(hits));
  return (
    <Panel title={t("v3.kb.retrieve")} end={hits ? <span className="mono">{t("v3.kb.hits", { count: hits.length })}</span> : undefined}>
      <p style={{ margin: "0 0 12px", color: "var(--v3-text-3)", fontSize: 13 }}>{t("v3.kb.retrieveSub")}</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run();
        }}
        style={{ display: "flex", gap: 8 }}
      >
        <Reasoned disabledReason={t("v3.kb.retrieveNotReady")}>
          <input className="v3-input" value={q} onChange={(e) => setQ(e.target.value)} disabled={!ready}
            placeholder={t("knowledge.detail.playground.queryPlaceholder")} aria-label={t("knowledge.detail.playground.query")} />
        </Reasoned>
        <Select className="v3-select" style={{ width: 96 }} value={n} onChange={(e) => setN(Number(e.target.value))} aria-label={t("v3.kb.resultCount")}>
          {[3, 5, 10, 20, 50, KB_QUERY_MAX_RESULTS].map((v) => <option key={v} value={v}>{v}</option>)}
        </Select>
        <Btn kind="primary" type="submit" busy={busy} disabled={!ready || !q.trim()}
          disabledReason={!ready ? t("v3.kb.retrieveNotReady") : t("story.reason.typeQuery")}><Search size={14} /> {t("v3.kb.search2")}</Btn>
      </form>
      {!ready && <div style={{ marginTop: 12 }}><Notice s="wait">{t("v3.kb.retrieveNotReady")}</Notice></div>}
      {error != null && <div style={{ marginTop: 12 }}><Notice s="act"><ApiErrorDetail error={error} /></Notice></div>}
      {hits && scoresUninformative(hits) && (
        <div style={{ marginTop: 12 }} data-testid="v3-kb-score-uninformative"><Notice s="wait">{t("v2.knowledge.scoreUninformative")}</Notice></div>
      )}
      {hits && (
        <div style={{ display: "grid", gap: 10, marginTop: 14 }}>
          {hits.length === 0 ? (
            <Empty title={t("v3.kb.noHits")} />
          ) : (
            hits.map((h, i) => (
              <div key={i} className="v3-hit">
                <div className="head">
                  <span>#{i + 1}</span>
                  <span className="uri" title={h.location_uri ?? undefined}>{h.location_uri ?? "—"}</span>
                  {showScores && typeof h.score === "number" && <span style={{ color: "var(--v3-info)" }} title={t("v2.knowledge.scoreHint")}>{h.score.toFixed(3)}</span>}
                </div>
                {showScores && typeof h.score === "number" && <div className="bar"><i style={{ width: `${(h.score / top) * 100}%` }} /></div>}
                <div className="text">{h.text}</div>
              </div>
            ))
          )}
        </div>
      )}
    </Panel>
  );
}

function AddSource({ kbId, onClose, onDone }: { kbId: string; onClose: () => void; onDone: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [mode, setMode] = useState<"upload" | "existing">("upload");
  const [files, setFiles] = useState<File[]>([]);
  const [bucket, setBucket] = useState("");
  const [prefix, setPrefix] = useState("");
  const [busy, setBusy] = useState(false);
  // a validation string, or the ApiError itself so the notice can show AWS details
  const [error, setError] = useState<unknown>(null);
  const submit = async () => {
    if (mode === "upload" && files.length === 0) return setError(t("knowledge.detail.sources.uploadEmpty"));
    if (mode === "existing" && !bucket.trim()) return setError(t("knowledge.detail.sources.bucketEmpty"));
    setError(null);
    setBusy(true);
    try {
      if (mode === "upload") {
        // files land in the KB's artifacts-bucket prefix, i.e. its upload source
        await v2KnowledgeApi.uploadFiles(kbId, files);
        toast("ok", t("knowledge.detail.sources.uploadedFiles", { n: files.length }));
      } else {
        await v2KnowledgeApi.addSource(kbId, { mode: "existing", bucket: bucket.trim(), prefix: prefix.trim() || undefined });
        toast("ok", t("knowledge.detail.sources.added"));
      }
      onDone();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      wide
      title={t("knowledge.detail.sources.add")}
      onClose={onClose}
      foot={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" onClick={() => void submit()} disabled={busy}>
            {busy ? t("knowledge.detail.sources.adding") : mode === "upload" ? t("knowledge.detail.sources.uploadSubmit") : t("knowledge.detail.sources.addSubmit")}
          </Btn>
        </>
      }
    >
      <div style={{ display: "grid", gap: 14 }}>
        <Filters
          value={mode}
          onChange={(m) => {
            setMode(m);
            setError(null);
          }}
          options={[
            { value: "upload", label: t("v3.kb.modeUpload") },
            { value: "existing", label: t("v3.kb.modeExisting") },
          ]}
        />
        {mode === "upload" ? (
          <label className="v3-field">
            <span>{t("v3.kb.files")}</span>
            <input type="file" multiple className="v3-input" style={{ paddingTop: 6 }}
              onChange={(e) => setFiles((prev) => mergeFiles(prev, Array.from(e.target.files ?? [])))} />
            {files.length > 0 && (
              <span style={{ textTransform: "none", letterSpacing: 0, fontFamily: "var(--v3-body)", color: "var(--v3-text-2)", fontSize: 13 }}>
                {files.map((f) => `${f.name} (${formatBytes(f.size)})`).join(" · ")}
              </span>
            )}
          </label>
        ) : (
          <>
            <label className="v3-field"><span>{t("v3.kb.bucket")}</span><input className="v3-input" value={bucket} onChange={(e) => setBucket(e.target.value)} placeholder="my-bucket" /></label>
            <label className="v3-field"><span>{t("v3.kb.prefix")}</span><input className="v3-input" value={prefix} onChange={(e) => setPrefix(e.target.value)} placeholder="docs/" /></label>
            <Notice>{t("v3.kb.existingHint")}</Notice>
          </>
        )}
        {error != null && <Notice s="act"><ApiErrorDetail error={error} /></Notice>}
      </div>
    </Dialog>
  );
}

function EditDescription({ kb, onClose, onSaved }: { kb: KnowledgeBaseDetail; onClose: () => void; onSaved: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [draft, setDraft] = useState(kb.description);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await v2KnowledgeApi.updateDescription(kb.kb_id, draft);
      toast("ok", t("v2.knowledge.descSaved"));
      onSaved();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t("v2.knowledge.editDesc")}
      onClose={onClose}
      foot={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" onClick={() => void save()} disabled={busy}>{t("v3.kb.save")}</Btn>
        </>
      }
    >
      <textarea className="v3-input" style={{ minHeight: 140 }} value={draft} maxLength={KB_DESCRIPTION_MAX}
        onChange={(e) => setDraft(e.target.value)} placeholder={t("knowledge.create.descriptionPlaceholder")} />
      <p style={{ margin: "8px 0 0", fontSize: 12, color: "var(--v3-text-3)" }}>
        {t("knowledge.create.descriptionHint")} ({draft.length}/{KB_DESCRIPTION_MAX})
      </p>
      {error != null && <div style={{ marginTop: 12 }}><Notice s="act"><ApiErrorDetail error={error} /></Notice></div>}
    </Dialog>
  );
}

/** Source → ingest → indexed, for one data source, read from its latest job. */
function ingestNodes(ds: DataSource, t: (k: string, o?: Record<string, unknown>) => string): TrackNode[] {
  const job = ds.ingestion_jobs?.[0];
  const dsSignal = resourceSignal(ds.status);
  const jobSignal = job ? resourceSignal(job.status) : "off";
  const stats = job?.statistics ?? {};
  const indexed = (stats.numberOfNewDocumentsIndexed ?? 0) + (stats.numberOfModifiedDocumentsIndexed ?? 0);
  const failed = stats.numberOfDocumentsFailed ?? 0;
  return [
    { key: "source", label: t("v3.kb.stepSource"), detail: ds.status, s: dsSignal, here: dsSignal !== "ok" },
    {
      key: "ingest",
      label: t("v3.kb.stepIngest"),
      detail: job ? `${job.status} · ${ago(job.started_at)}` : t("v3.kb.neverSynced"),
      s: dsSignal !== "ok" ? "off" : jobSignal,
      here: dsSignal === "ok" && jobSignal !== "ok",
    },
    {
      key: "indexed",
      label: t("v3.kb.stepIndexed"),
      detail: job && jobSignal === "ok" ? t("v3.kb.indexedDetail", { indexed, failed, scanned: stats.numberOfDocumentsScanned ?? 0 }) : "—",
      s: jobSignal === "ok" ? (failed > 0 ? "wait" : "ok") : "off",
      here: jobSignal === "ok",
    },
  ];
}

function KbDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useToast();
  const failed = useFailure();
  const statusLabel = useKbStatus();
  const [params, setParams] = useSearchParams();
  const [kb, setKb] = useState<KnowledgeBaseDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [gone, setGone] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);
  const [syncing, setSyncing] = useState<string | null>(null);
  const [repairing, setRepairing] = useState(false);
  const [removeDs, setRemoveDs] = useState<DataSource | null>(null);
  const [removing, setRemoving] = useState(false);
  const [dialog, setDialog] = useState<"add" | "desc" | null>(null);
  const del = useKbDelete(() => navigate("/v3/knowledge"));
  const selected = params.get("ds");
  const agents = useLoad(() => api.listAgents(), "v3-kb-agents");
  const agentIds = useMemo(() => new Map((agents.data?.agents ?? []).map((a) => [a.name, a.id])), [agents.data]);

  const loadDetail = useCallback(async (): Promise<KnowledgeBaseDetail | null> => {
    try {
      const fresh = await v2KnowledgeApi.get(id);
      setKb(fresh);
      setError(null);
      return fresh;
    } catch (err) {
      setError(err);
      if (err instanceof ApiError && GONE_CODES.has(err.code)) setGone(true);
      return null;
    }
  }, [id]);

  // create-flow automation, each step at most once per mount
  const autoSynced = useRef<Set<string>>(new Set());
  const sourceWaitTicks = useRef(0);
  const awaitingBackendSource = useCallback((d: KnowledgeBaseDetail): boolean => {
    if (String(d.status).toUpperCase() !== "ACTIVE" || d.data_sources.length > 0) {
      sourceWaitTicks.current = 0;
      return false;
    }
    sourceWaitTicks.current += 1;
    return sourceWaitTicks.current <= SOURCE_WAIT_TICKS;
  }, []);
  // the first ingestion starts on its own once a data source is AVAILABLE with no jobs
  const autoFirstSync = useCallback(
    async (d: KnowledgeBaseDetail): Promise<boolean> => {
      let fired = false;
      for (const ds of d.data_sources) {
        if (ds.status.toUpperCase() === "AVAILABLE" && (ds.ingestion_jobs ?? []).length === 0 && !autoSynced.current.has(ds.ds_id)) {
          autoSynced.current.add(ds.ds_id);
          fired = true;
          try {
            await v2KnowledgeApi.sync(id, ds.ds_id);
            toast("ok", t("knowledge.detail.sources.syncStarted"));
          } catch {
            // "sync now" stays available
          }
        }
      }
      return fired;
    },
    [id, t, toast],
  );
  // poll while anything is in flight; a mutation bumps refreshKey and restarts the loop
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      const d = await loadDetail();
      if (cancelled || !d) return;
      const pending = awaitingBackendSource(d);
      const synced = await autoFirstSync(d);
      if (cancelled) return;
      if (kbInFlight(d) || pending || synced) timer = window.setTimeout(() => void tick(), POLL_MS);
    };
    void tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [loadDetail, refreshKey, awaitingBackendSource, autoFirstSync]);

  if (gone) {
    return (
      <Notice s="wait">
        {t("staleLink.body", { kind: t("staleLink.kind.knowledgeBase"), id })}{" "}
        <Link to="/v3/knowledge" style={{ textDecoration: "underline" }}>{t("v3.kb.title")}</Link>
      </Notice>
    );
  }
  if (error != null && !kb) return <Notice s="act">{t("knowledge.detail.loadFailedTitle")}: <ApiErrorDetail error={error} /></Notice>;
  if (!kb) return <Skeleton rows={6} />;

  const sources = kb.data_sources;
  const current = sources.find((ds) => ds.ds_id === selected) ?? sources[0] ?? null;
  const s = kbSignal(kb.status);
  const provisioning = String(kb.status).toUpperCase() === "CREATING";
  const missingSource = !provisioning && String(kb.status).toUpperCase() === "ACTIVE" && sources.length === 0;
  const totalDocs = sources.reduce((sum, ds) => {
    const st = ds.ingestion_jobs?.[0]?.statistics ?? {};
    return sum + (st.numberOfDocumentsScanned ?? 0);
  }, 0);

  const sync = async (ds: DataSource) => {
    setSyncing(ds.ds_id);
    try {
      failed.clear();
      await v2KnowledgeApi.sync(id, ds.ds_id);
      toast("ok", t("knowledge.detail.sources.syncStarted"));
      refresh();
    } catch (err) {
      failed.fail(err, t("knowledge.detail.sources.syncFailed", { msg: errorMessage(err) }));
    } finally {
      setSyncing(null);
    }
  };
  // idempotent server-side, so a stray click cannot create a second source
  const repair = async () => {
    setRepairing(true);
    try {
      failed.clear();
      await v2KnowledgeApi.addSource(id, { mode: "upload" });
      toast("ok", t("knowledge.detail.sources.autoCreated"));
      sourceWaitTicks.current = 0;
      refresh();
    } catch (err) {
      failed.fail(err, t("common.actionFailed", { msg: errorMessage(err) }));
    } finally {
      setRepairing(false);
    }
  };
  const removeSource = async () => {
    if (!removeDs) return;
    setRemoving(true);
    try {
      failed.clear();
      await v2KnowledgeApi.removeSource(id, removeDs.ds_id);
      toast("ok", t("knowledge.detail.sources.deleted"));
      setRemoveDs(null);
      refresh();
    } catch (err) {
      failed.fail(err, t("common.actionFailed", { msg: errorMessage(err) }));
    } finally {
      setRemoving(false);
    }
  };

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <div>
        <button type="button" className="v3-btn ghost sm" onClick={() => navigate("/v3/knowledge")}>
          <ArrowLeft size={14} /> {t("v3.kb.title")}
        </button>
      </div>
      <PageHead
        eyebrow={`${t("v3.kb.eyebrow")} · ${kb.kb_id}`}
        title={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 14 }}>
            <Lamp s={s} live={s === "ok" || s === "wait"} />
            {kb.name}
          </span>
        }
        sub={
          <span style={{ display: "inline-flex", gap: 8, alignItems: "flex-start" }}>
            <span>{kb.description || t("v3.kb.noDescription")}</span>
            <button type="button" className="v3-btn ghost sm" aria-label={t("v2.knowledge.editDesc")} onClick={() => setDialog("desc")}><Pencil size={13} /></button>
          </span>
        }
        end={
          <>
            <Btn kind="ghost" onClick={refresh}><RefreshCw size={14} /></Btn>
            <Btn kind="danger" disabled={kbDeleteBlockedReason(kb.status) !== null} disabledReason={t(kbDeleteBlockedReason(kb.status) ?? "story.reason.deleting")} onClick={() => del.ask(kb)}><Trash2 size={14} /> {t("v3.kb.delete")}</Btn>
            <Btn kind="primary" onClick={() => setDialog("add")}><Upload size={14} /> {t("v3.kb.addSource")}</Btn>
          </>
        }
      />
      <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-kb-error" />
      <ErrorCallout error={del.failed.error} onDismiss={del.failed.clear} data-testid="v3-kb-delete-error" />

      {kb.failure_reasons && kb.failure_reasons.length > 0 && <Notice s="act">{kb.failure_reasons.join("; ")}</Notice>}
      {error != null && <Notice s="wait"><ApiErrorDetail error={error} /></Notice>}
      {provisioning && <Notice s="wait">{t("knowledge.detail.sources.provisioning")}</Notice>}
      {missingSource && (
        <Notice s="wait">
          {t("knowledge.detail.sources.missingSource")}{" "}
          <Btn size="sm" disabled={repairing} onClick={() => void repair()}>
            {repairing ? t("knowledge.detail.sources.adding") : t("knowledge.detail.sources.repairSource")}
          </Btn>
        </Notice>
      )}

      <div className="v3-grid c4">
        <Panel signal={s === "off" ? undefined : s}><Stat label={t("v3.kb.status")} value={statusLabel(kb.status)} signal={s === "off" ? undefined : s} /></Panel>
        <Panel><Stat label={t("v3.kb.sourcesLabel")} value={sources.length} /></Panel>
        <Panel><Stat label={t("v3.kb.scanned")} value={totalDocs} foot={t("v3.kb.scannedFoot")} /></Panel>
        <Panel><Stat label={t("v3.kb.mounted")} value={kb.attached_agents.length} foot={ago(kb.updated_at)} /></Panel>
      </div>

      {current && (
        <Panel title={t("v3.kb.ingestOf", { name: current.name })} signal={resourceSignal(current.ingestion_jobs?.[0]?.status ?? current.status) === "act" ? "act" : undefined}>
          <Track nodes={ingestNodes(current, t)} />
          {current.ingestion_jobs?.[0]?.failure_reasons?.length ? (
            <div style={{ marginTop: 12 }}><Notice s="act">{current.ingestion_jobs[0].failure_reasons.join("; ")}</Notice></div>
          ) : null}
          {current.ingestion_jobs?.[0]?.statistics && (
            <div style={{ display: "flex", gap: 18, flexWrap: "wrap", marginTop: 14, fontFamily: "var(--v3-mono)", fontSize: 12, color: "var(--v3-text-3)" }}>
              {Object.entries(current.ingestion_jobs[0].statistics).map(([k, v]) => (
                <span key={k}>{t(`knowledge.stats.${k}`, { defaultValue: humanizeStat(k) })} <b style={{ color: "var(--v3-text)", fontWeight: 500 }}>{v}</b></span>
              ))}
            </div>
          )}
        </Panel>
      )}

      <div className="v3-grid v3-split">
        <Panel title={t("v3.kb.sourcesLabel")} flush>
          {sources.length === 0 ? (
            <Empty title={t("v3.kb.noSources")} />
          ) : (
            <table className="v3-table">
              <tbody>
                {sources.map((ds) => {
                  const available = ds.status.toUpperCase() === "AVAILABLE";
                  const running = (ds.ingestion_jobs ?? []).some((j) => jobRunning(j));
                  const job = ds.ingestion_jobs?.[0];
                  const on = current?.ds_id === ds.ds_id;
                  return (
                    <tr key={ds.ds_id} className="click" style={on ? { background: "var(--v3-ink-3)" } : undefined}
                      onClick={() => setParams({ id, ds: ds.ds_id }, { replace: true })}>
                      <td style={{ width: 30 }}><Lamp s={running ? "wait" : resourceSignal(job?.status ?? ds.status)} live={running} /></td>
                      <td>
                        <div className="v3-name"><div><b>{ds.name}</b><small>{ds.bucket ? `s3://${ds.bucket}/${ds.prefix ?? ""}` : "—"}</small></div></div>
                      </td>
                      <td style={{ color: "var(--v3-text-3)" }}>{job ? `${job.status} · ${ago(job.started_at)}` : t("v3.kb.neverSynced")}</td>
                      <td style={{ width: 1, whiteSpace: "nowrap" }} onClick={(e) => e.stopPropagation()}>
                        <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                          <Btn size="sm" busy={syncing === ds.ds_id} disabled={!available || running}
                            disabledReason={!available ? t("knowledge.detail.sources.syncNotReady") : t("knowledge.detail.sources.syncRunning")}
                            onClick={() => void sync(ds)}>
                            <RefreshCw size={13} /> {syncing === ds.ds_id ? t("knowledge.detail.sources.syncing") : t("knowledge.detail.sources.syncNow")}
                          </Btn>
                          <Btn size="sm" kind="ghost" onClick={() => setRemoveDs(ds)} title={t("knowledge.detail.sources.removeSource")}><Trash2 size={13} /></Btn>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Panel>
        <Panel title={t("knowledge.detail.agents.title")}>
          {kb.attached_agents.length === 0 ? (
            <p style={{ margin: 0, color: "var(--v3-text-3)" }}>{t("v3.kb.noAgents")}</p>
          ) : (
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              {kb.attached_agents.map((name) => {
                const agentId = agentIds.get(name);
                return agentId ? (
                  <Link key={name} to={`/v3/agents?id=${agentId}`} className="v3-chip" data-s="info">{name}</Link>
                ) : (
                  <Chip key={name}>{name}</Chip>
                );
              })}
            </div>
          )}
        </Panel>
      </div>

      <Retrieve kb={kb} />
      {current && <Documents key={current.ds_id} kbId={kb.kb_id} ds={current} />}

      {dialog === "add" && <AddSource kbId={kb.kb_id} onClose={() => setDialog(null)} onDone={() => { setDialog(null); refresh(); }} />}
      {dialog === "desc" && <EditDescription kb={kb} onClose={() => setDialog(null)} onSaved={() => { setDialog(null); refresh(); }} />}
      {removeDs && (
        <Confirm
          title={t("knowledge.detail.sources.removeTitle")}
          confirmLabel={t("knowledge.detail.sources.removeSource")}
          cancelLabel={t("v3.common.cancel")}
          danger
          busy={removing}
          onCancel={() => setRemoveDs(null)}
          onConfirm={() => void removeSource()}
        >
          {t("knowledge.detail.sources.removeBody", { name: removeDs.name })}
        </Confirm>
      )}
      {del.dialog}
    </div>
  );
}

export function V3Knowledge() {
  const [params] = useSearchParams();
  const id = params.get("id");
  return id ? <KbDetail key={id} id={id} /> : <KbShelf />;
}
