import type { CSSProperties } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Chip, ConfirmDialog, Panel, useToast, ViewHead } from "../../components";
import {
  KbStatusChip,
  type DataSource,
  type IngestionJob,
  type KnowledgeBaseDetail,
  type QueryResultItem,
} from "../KnowledgeBases";
import {
  extractConflictAgents,
  formatBytes,
  kbErrorMessage,
  resourceTone,
} from "./kb-helpers";
import { SourcePicker, type SourceMode } from "./SourcePicker";

interface DetailViewProps {
  kbId: string;
  onBack: () => void;
  /** The detail fetch answered 4xx (not found / validation / access denied) — the id is gone. */
  onGone?: () => void;
}

// A KB is "in flight" (worth polling) while it is provisioning, any data source
// is being created/deleted, or any ingestion job is still running.
function isInFlight(d: KnowledgeBaseDetail): boolean {
  if (["CREATING", "DELETING", "UPDATING"].includes(String(d.status).toUpperCase())) return true;
  for (const ds of d.data_sources) {
    if (["CREATING", "DELETING"].includes(ds.status.toUpperCase())) return true;
    for (const j of ds.ingestion_jobs ?? []) {
      if (["IN_PROGRESS", "STARTING", "STOPPING"].includes(j.status.toUpperCase())) return true;
    }
  }
  return false;
}

// "numberOfDocumentsScanned" → "Documents Scanned" (AWS stat keys, shown raw).
function humanizeStat(key: string): string {
  return key.replace(/^numberOf/, "").replace(/([A-Z])/g, " $1").trim();
}

interface KBDocument {
  name: string;
  uri: string;
  status: string;
  status_reason?: string | null;
  indexed_at?: string | null;
  size_bytes?: number | null;
  uploaded_at?: string | null;
}

const DOC_PAGE_SIZE = 50;

// Per-source document listing — loaded lazily on expand; the backend page is
// token-based (ListKnowledgeBaseDocuments), so pagination is a LOAD MORE that
// appends the next page.
function SourceDocuments({ kbId, dsId }: { kbId: string; dsId: string }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [docs, setDocs] = useState<KBDocument[] | null>(null);
  const [nextToken, setNextToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadPage = async (token: string | null) => {
    setLoading(true);
    setError(null);
    try {
      const qs = new URLSearchParams({ page_size: String(DOC_PAGE_SIZE) });
      if (token) qs.set("token", token);
      const res = await fetch(
        `/api/knowledge-bases/${kbId}/data-sources/${dsId}/documents?${qs.toString()}`,
      );
      const body = (await res.json().catch(() => ({}))) as {
        documents?: KBDocument[];
        next_token?: string | null;
        message?: string;
      };
      if (!res.ok) {
        setError(kbErrorMessage(body, res.status));
        return;
      }
      setDocs((prev) =>
        token ? [...(prev ?? []), ...(body.documents ?? [])] : (body.documents ?? []),
      );
      setNextToken(body.next_token ?? null);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  };

  const linkStyle: CSSProperties = {
    background: "none",
    border: 0,
    color: "var(--amber)",
    cursor: "pointer",
    fontFamily: "var(--mono)",
    fontSize: 10,
    padding: 0,
  };

  return (
    <div style={{ marginTop: 12 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <button
          type="button"
          style={{ ...linkStyle, letterSpacing: ".14em" }}
          onClick={() => {
            const next = !open;
            setOpen(next);
            if (next && docs === null) void loadPage(null);
          }}
          data-testid="kb-docs-toggle"
        >
          ▤ {t("knowledge.detail.sources.documents")} {open ? "▾" : "▸"}
          {docs !== null && ` (${docs.length}${nextToken ? "+" : ""})`}
        </button>
        {open && docs !== null && (
          <button
            type="button"
            style={linkStyle}
            onClick={() => void loadPage(null)}
            data-testid="kb-docs-refresh"
          >
            ⟳ {t("knowledge.detail.sources.docsRefresh")}
          </button>
        )}
      </div>

      {open && (
        <div style={{ marginTop: 8 }}>
          {error && (
            <div className="note" style={{ color: "var(--crit)", borderColor: "var(--crit)" }}>
              <span className="i">!</span>
              <span>{error}</span>
            </div>
          )}
          {docs !== null && docs.length === 0 && !loading && !error && (
            <p className="dim" style={{ fontSize: 12, margin: 0 }}>
              {t("knowledge.detail.sources.docsEmpty")}
            </p>
          )}
          {docs !== null && docs.length > 0 && (
            <table data-testid="kb-docs-table">
              <thead>
                <tr>
                  <th>{t("knowledge.detail.sources.docName")}</th>
                  <th>{t("knowledge.detail.sources.docSize")}</th>
                  <th>{t("knowledge.detail.sources.docUploaded")}</th>
                  <th>{t("knowledge.detail.sources.docStatus")}</th>
                  <th>{t("knowledge.detail.sources.docIndexed")}</th>
                </tr>
              </thead>
              <tbody>
                {docs.map((doc) => (
                  <tr key={doc.uri}>
                    <td title={doc.uri} style={{ wordBreak: "break-all" }}>
                      {doc.name}
                    </td>
                    <td className="mono">{formatBytes(doc.size_bytes)}</td>
                    <td className="mono">{doc.uploaded_at?.slice(0, 19).replace("T", " ") ?? "—"}</td>
                    <td>
                      <Chip tone={resourceTone(doc.status)}>{doc.status}</Chip>
                      {doc.status_reason && (
                        <span className="dim" style={{ fontSize: 10.5, marginLeft: 6 }} title={doc.status_reason}>
                          !
                        </span>
                      )}
                    </td>
                    <td className="mono">{doc.indexed_at?.slice(0, 19).replace("T", " ") ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {loading && <div className="loading-line">{t("common.loading")}</div>}
          {!loading && nextToken && (
            <div style={{ marginTop: 8 }}>
              <Btn onClick={() => void loadPage(nextToken)} data-testid="kb-docs-more">
                {t("knowledge.detail.sources.docsLoadMore")} ▸
              </Btn>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function DetailView({ kbId, onBack, onGone }: DetailViewProps) {
  const { t } = useTranslation();
  const toast = useToast();

  const [detail, setDetail] = useState<KnowledgeBaseDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  // description inline edit
  const [editingDesc, setEditingDesc] = useState(false);
  const [descDraft, setDescDraft] = useState("");
  const [savingDesc, setSavingDesc] = useState(false);

  // delete flow
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [conflict, setConflict] = useState<{ agents: string[] } | null>(null);
  const [deleting, setDeleting] = useState(false);

  // add data source
  const [showAdd, setShowAdd] = useState(false);
  const [addMode, setAddMode] = useState<SourceMode>("upload");
  const [addFiles, setAddFiles] = useState<File[]>([]);
  const [addBucket, setAddBucket] = useState("");
  const [addPrefix, setAddPrefix] = useState("");
  const [addBusy, setAddBusy] = useState(false);
  const [repairing, setRepairing] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  // per-source sync + delete
  const [syncing, setSyncing] = useState<string | null>(null);
  const [confirmDeleteDs, setConfirmDeleteDs] = useState<DataSource | null>(null);

  // playground
  const [queryText, setQueryText] = useState("");
  const [numResults, setNumResults] = useState(8);
  const [querying, setQuerying] = useState(false);
  const [results, setResults] = useState<QueryResultItem[] | null>(null);
  const [queryError, setQueryError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const loadDetail = useCallback(async (): Promise<KnowledgeBaseDetail | null> => {
    if (!kbId) return null;
    try {
      const res = await fetch(`/api/knowledge-bases/${kbId}`);
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { message?: string };
        setLoadError(kbErrorMessage(body, res.status));
        // any 4xx (aws.not_found / aws.validation / aws.access_denied) means
        // the linked id does not resolve in this workspace — 401 is auth
        if (res.status >= 400 && res.status < 500 && res.status !== 401) onGone?.();
        return null;
      }
      const body = (await res.json()) as KnowledgeBaseDetail;
      setLoadError(null);
      setDetail(body);
      return body;
    } catch (err) {
      setLoadError(String(err));
      return null;
    }
  }, [kbId, onGone]);

  // Create-flow automation guards (fire each step at most once per mount).
  const autoSynced = useRef<Set<string>>(new Set());
  const sourceWaitTicks = useRef(0);

  // A slow create (KB still CREATING when the request returned) has its data
  // source finished by a backend thread, so nothing is replayed from here.
  // Keep polling for a bounded window so the source appears on its own; past
  // that the banner below offers a manual repair.
  const awaitingBackendSource = useCallback((d: KnowledgeBaseDetail): boolean => {
    if (String(d.status).toUpperCase() !== "ACTIVE" || d.data_sources.length > 0) {
      sourceWaitTicks.current = 0;
      return false;
    }
    sourceWaitTicks.current += 1;
    return sourceWaitTicks.current <= 40; // ~3.5 min at the 5 s poll interval
  }, []);

  // PRD: the first ingestion starts automatically once a data source is
  // AVAILABLE — trigger it when a source has no jobs yet.
  const autoFirstSync = useCallback(
    async (d: KnowledgeBaseDetail): Promise<boolean> => {
      let fired = false;
      for (const ds of d.data_sources) {
        if (
          ds.status.toUpperCase() === "AVAILABLE" &&
          (ds.ingestion_jobs ?? []).length === 0 &&
          !autoSynced.current.has(ds.ds_id)
        ) {
          autoSynced.current.add(ds.ds_id);
          fired = true;
          const res = await fetch(
            `/api/knowledge-bases/${kbId}/data-sources/${ds.ds_id}/sync`,
            { method: "POST" },
          );
          if (res.ok) toast(t("knowledge.detail.sources.syncStarted"));
        }
      }
      return fired;
    },
    [kbId, t, toast],
  );

  // Poll while the KB has anything in flight; stop once quiescent. A mutation
  // bumps refreshKey which restarts the loop (re-fetch + resume polling).
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      const d = await loadDetail();
      if (cancelled || !d) return;
      const pending = awaitingBackendSource(d);
      const synced = await autoFirstSync(d);
      if (cancelled) return;
      if (isInFlight(d) || pending || synced) {
        timer = window.setTimeout(() => void tick(), 5000);
      }
    };
    void tick();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [loadDetail, refreshKey, awaitingBackendSource, autoFirstSync]);

  const saveDesc = async () => {
    setSavingDesc(true);
    try {
      const res = await fetch(`/api/knowledge-bases/${kbId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ description: descDraft }),
      });
      const body = (await res.json().catch(() => ({}))) as KnowledgeBaseDetail & { message?: string };
      if (!res.ok) {
        toast(t("common.actionFailed", { msg: kbErrorMessage(body, res.status) }));
        return;
      }
      setDetail((prev) => (prev ? { ...prev, description: descDraft } : prev));
      setEditingDesc(false);
    } catch (err) {
      toast(t("common.actionFailed", { msg: String(err) }));
    } finally {
      setSavingDesc(false);
    }
  };

  const doDelete = async (force: boolean) => {
    setDeleting(true);
    try {
      const res = await fetch(`/api/knowledge-bases/${kbId}?force=${force}`, { method: "DELETE" });
      if (res.ok) {
        toast(t("knowledge.detail.deleted", { name: detail?.name ?? "" }));
        onBack();
        return;
      }
      const body = (await res.json().catch(() => ({}))) as { code?: string; message?: string };
      const agents = extractConflictAgents(body);
      // 409 with attached agents → offer the force path; other 409s (e.g. the KB
      // is still provisioning) are just surfaced as a message.
      if (res.status === 409 && (body.code === "kb.has_attached_agents" || agents.length > 0)) {
        setConfirmDelete(false);
        setConflict({ agents });
        return;
      }
      toast(t("common.actionFailed", { msg: kbErrorMessage(body, res.status) }));
    } catch (err) {
      toast(t("common.actionFailed", { msg: String(err) }));
    } finally {
      setDeleting(false);
    }
  };

  // Repair for a KB that ended up ACTIVE with no data source (the backend
  // completion thread died with the process, or the KB predates that fix).
  // Idempotent server-side, so a stray click cannot create a second source.
  const repairMissingSource = async () => {
    setRepairing(true);
    try {
      const res = await fetch(`/api/knowledge-bases/${kbId}/data-sources`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "upload" }),
      });
      const body = (await res.json().catch(() => ({}))) as { message?: string };
      if (!res.ok) {
        toast(t("common.actionFailed", { msg: kbErrorMessage(body, res.status) }));
        return;
      }
      toast(t("knowledge.detail.sources.autoCreated"));
      sourceWaitTicks.current = 0;
      refresh();
    } catch (err) {
      toast(t("common.actionFailed", { msg: String(err) }));
    } finally {
      setRepairing(false);
    }
  };

  const submitAddSource = async () => {
    setAddBusy(true);
    setAddError(null);
    try {
      if (addMode === "upload") {
        if (addFiles.length === 0) {
          setAddError(t("knowledge.detail.sources.uploadEmpty"));
          return;
        }
        const form = new FormData();
        for (const f of addFiles) form.append("files", f);
        const res = await fetch(`/api/knowledge-bases/${kbId}/files`, { method: "POST", body: form });
        if (!res.ok) {
          const body = (await res.json().catch(() => ({}))) as { message?: string };
          setAddError(kbErrorMessage(body, res.status));
          return;
        }
        toast(t("knowledge.detail.sources.uploadedFiles", { n: addFiles.length }));
      } else {
        if (!addBucket.trim()) {
          setAddError(t("knowledge.detail.sources.bucketEmpty"));
          return;
        }
        const res = await fetch(`/api/knowledge-bases/${kbId}/data-sources`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            mode: "existing",
            bucket: addBucket.trim(),
            prefix: addPrefix.trim() || undefined,
          }),
        });
        if (!res.ok) {
          const body = (await res.json().catch(() => ({}))) as { message?: string };
          setAddError(kbErrorMessage(body, res.status));
          return;
        }
        toast(t("knowledge.detail.sources.added"));
      }
      setShowAdd(false);
      setAddFiles([]);
      setAddBucket("");
      setAddPrefix("");
      refresh();
    } catch (err) {
      setAddError(String(err));
    } finally {
      setAddBusy(false);
    }
  };

  const syncSource = async (ds: DataSource) => {
    setSyncing(ds.ds_id);
    try {
      const res = await fetch(`/api/knowledge-bases/${kbId}/data-sources/${ds.ds_id}/sync`, {
        method: "POST",
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { message?: string };
        toast(t("knowledge.detail.sources.syncFailed", { msg: kbErrorMessage(body, res.status) }));
        return;
      }
      toast(t("knowledge.detail.sources.syncStarted"));
      refresh();
    } catch (err) {
      toast(t("knowledge.detail.sources.syncFailed", { msg: String(err) }));
    } finally {
      setSyncing(null);
    }
  };

  const deleteSource = async (ds: DataSource) => {
    try {
      const res = await fetch(`/api/knowledge-bases/${kbId}/data-sources/${ds.ds_id}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { message?: string };
        toast(t("common.actionFailed", { msg: kbErrorMessage(body, res.status) }));
        return;
      }
      toast(t("knowledge.detail.sources.deleted"));
      refresh();
    } catch (err) {
      toast(t("common.actionFailed", { msg: String(err) }));
    }
  };

  const runQuery = async () => {
    const text = queryText.trim();
    if (!text) return;
    setQuerying(true);
    setQueryError(null);
    setResults(null);
    setExpanded(new Set());
    try {
      const res = await fetch(`/api/knowledge-bases/${kbId}/query`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, number_of_results: numResults }),
      });
      const body = (await res.json().catch(() => ({}))) as {
        results?: QueryResultItem[];
        message?: string;
      };
      if (!res.ok) {
        setQueryError(kbErrorMessage(body, res.status));
        return;
      }
      setResults(body.results ?? []);
    } catch (err) {
      setQueryError(String(err));
    } finally {
      setQuerying(false);
    }
  };

  const toggleExpanded = (i: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });

  if (loadError && !detail) {
    return (
      <section>
        <div style={{ marginBottom: 14 }}>
          <Btn onClick={onBack}>◂ {t("knowledge.title")}</Btn>
        </div>
        <Panel title={t("knowledge.detail.loadFailedTitle")}>
          <div className="note" style={{ color: "var(--crit)", borderColor: "var(--crit)" }}>
            <span className="i">!</span>
            <span>{loadError}</span>
          </div>
        </Panel>
      </section>
    );
  }

  if (!detail) {
    return (
      <section>
        <div style={{ marginBottom: 14 }}>
          <Btn onClick={onBack}>◂ {t("knowledge.title")}</Btn>
        </div>
        <div className="loading-line">{t("common.loading")}</div>
      </section>
    );
  }

  const provisioning = detail.status === "CREATING";

  return (
    <section>
      <ViewHead
        kicker={t("knowledge.kicker")}
        title={detail.name}
        meta={t("knowledge.detail.meta")}
      />
      <div style={{ marginBottom: 14 }}>
        <Btn onClick={onBack}>◂ {t("knowledge.title")}</Btn>
      </div>

      <div className="eval-grid">
        {/* ── Overview ─────────────────────────────────────────────────── */}
        <Panel
          brk
          title={t("knowledge.detail.overview.title")}
          end={<KbStatusChip status={detail.status} />}
          style={{ "--i": 0 } as CSSProperties}
        >
          <div className="kv">
            <span className="k">{t("knowledge.detail.overview.kbId")}</span>
            <span className="v">{detail.kb_id}</span>
          </div>
          {detail.arn && (
            <div className="kv">
              <span className="k">{t("knowledge.detail.overview.arn")}</span>
              <span className="v" style={{ wordBreak: "break-all" }}>
                {detail.arn}
              </span>
            </div>
          )}
          <div className="kv">
            <span className="k">{t("knowledge.detail.overview.updated")}</span>
            <span className="v">{detail.updated_at?.slice(0, 19) ?? "—"}</span>
          </div>
          {detail.failure_reasons && detail.failure_reasons.length > 0 && (
            <div
              className="note"
              style={{ marginTop: 10, color: "var(--crit)", borderColor: "var(--crit)" }}
            >
              <span className="i">!</span>
              <span>{detail.failure_reasons.join("; ")}</span>
            </div>
          )}

          <div style={{ marginTop: 12 }}>
            <div
              style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}
            >
              <label
                style={{
                  fontFamily: "var(--mono)",
                  fontSize: 9.5,
                  letterSpacing: ".18em",
                  color: "var(--ink-3)",
                }}
              >
                {t("knowledge.detail.overview.description")}
              </label>
              {!editingDesc && (
                <button
                  type="button"
                  onClick={() => {
                    setDescDraft(detail.description);
                    setEditingDesc(true);
                  }}
                  style={{
                    background: "none",
                    border: 0,
                    color: "var(--amber)",
                    cursor: "pointer",
                    fontFamily: "var(--mono)",
                    fontSize: 10,
                  }}
                  data-testid="kb-desc-edit"
                >
                  {t("knowledge.detail.overview.editDesc")}
                </button>
              )}
            </div>
            {editingDesc ? (
              <div style={{ marginTop: 7 }}>
                <textarea
                  className="input"
                  style={{ minHeight: 72, resize: "vertical" }}
                  value={descDraft}
                  onChange={(e) => setDescDraft(e.target.value)}
                  data-testid="kb-desc-textarea"
                />
                <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 8 }}>
                  <Btn disabled={savingDesc} onClick={() => setEditingDesc(false)}>
                    {t("common.cancel")}
                  </Btn>
                  <Btn primary disabled={savingDesc} onClick={() => void saveDesc()}>
                    {savingDesc ? t("knowledge.detail.overview.saving") : t("knowledge.detail.overview.saveDesc")}
                  </Btn>
                </div>
              </div>
            ) : (
              <p style={{ marginTop: 7, fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.6 }}>
                {detail.description || <span className="dim">{t("knowledge.detail.overview.noDescription")}</span>}
              </p>
            )}
          </div>

          <div style={{ marginTop: 14, display: "flex", justifyContent: "flex-end" }}>
            <Btn
              disabled={deleting}
              style={{ color: "var(--crit)", borderColor: "var(--crit)" }}
              onClick={() => setConfirmDelete(true)}
              data-testid="kb-delete-btn"
            >
              {t("knowledge.detail.overview.delete")}
            </Btn>
          </div>
        </Panel>

        {/* ── Attached agents ──────────────────────────────────────────── */}
        <Panel
          title={t("knowledge.detail.agents.title")}
          sub={t("knowledge.detail.agents.sub")}
          style={{ "--i": 1 } as CSSProperties}
        >
          {detail.attached_agents.length === 0 ? (
            <p className="dim" style={{ fontSize: 12 }}>
              {t("knowledge.detail.agents.empty")}
            </p>
          ) : (
            <div className="selchips" data-testid="kb-attached-agents">
              {detail.attached_agents.map((name) => (
                <span key={name} className="selchip" style={{ cursor: "default" }}>
                  {name}
                </span>
              ))}
            </div>
          )}
        </Panel>
      </div>

      {/* ── Data sources ─────────────────────────────────────────────────── */}
      <Panel
        brk
        title={t("knowledge.detail.sources.title")}
        sub={t("knowledge.detail.sources.sub")}
        end={
          <Btn onClick={() => setShowAdd((v) => !v)} data-testid="kb-add-source-btn">
            {showAdd ? t("common.cancel") : `+ ${t("knowledge.detail.sources.add")}`}
          </Btn>
        }
        style={{ marginTop: 14, "--i": 2 } as CSSProperties}
      >
        {provisioning && (
          <div className="note" style={{ marginBottom: 12 }}>
            <span className="i">[i]</span>
            <span>{t("knowledge.detail.sources.provisioning")}</span>
          </div>
        )}

        {/* ACTIVE with no source: the backend is still finishing it, or its
            completion was lost (process restart / KB predating that fix). Say so
            and offer the repair rather than looking like an empty KB. */}
        {!provisioning && detail.data_sources.length === 0 && (
          <div
            className="note"
            style={{ marginBottom: 12, borderColor: "var(--warn)" }}
            data-testid="kb-missing-source"
          >
            <span className="i">!</span>
            <span>
              {t("knowledge.detail.sources.missingSource")}{" "}
              <Btn onClick={() => void repairMissingSource()} disabled={repairing}>
                {repairing
                  ? t("knowledge.detail.sources.adding")
                  : t("knowledge.detail.sources.repairSource")}
              </Btn>
            </span>
          </div>
        )}

        {showAdd && (
          <div
            className="code"
            style={{ padding: 14, marginBottom: 14, whiteSpace: "normal" }}
            data-testid="kb-add-source-form"
          >
            <SourcePicker
              mode={addMode}
              onModeChange={setAddMode}
              files={addFiles}
              onFilesChange={setAddFiles}
              bucket={addBucket}
              onBucketChange={setAddBucket}
              prefix={addPrefix}
              onPrefixChange={setAddPrefix}
            />
            {addError && (
              <div
                className="note"
                style={{ color: "var(--crit)", borderColor: "var(--crit)" }}
                data-testid="kb-add-source-error"
              >
                <span className="i">!</span>
                <span>{addError}</span>
              </div>
            )}
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 12 }}>
              <Btn primary disabled={addBusy} onClick={() => void submitAddSource()}>
                {addBusy
                  ? t("knowledge.detail.sources.adding")
                  : addMode === "upload"
                    ? t("knowledge.detail.sources.uploadSubmit")
                    : t("knowledge.detail.sources.addSubmit")}
              </Btn>
            </div>
          </div>
        )}

        {detail.data_sources.length === 0 && !provisioning ? (
          <p className="dim" style={{ fontSize: 12 }}>
            {t("knowledge.detail.sources.empty")}
          </p>
        ) : (
          detail.data_sources.map((ds) => {
            const jobs = ds.ingestion_jobs ?? [];
            const available = ds.status.toUpperCase() === "AVAILABLE";
            const running = jobs.some((j) =>
              ["IN_PROGRESS", "STARTING"].includes(j.status.toUpperCase()),
            );
            return (
              <div
                key={ds.ds_id}
                style={{
                  border: "1px solid var(--grid)",
                  padding: 13,
                  marginBottom: 12,
                }}
                data-testid="kb-data-source"
              >
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    gap: 10,
                    alignItems: "center",
                    flexWrap: "wrap",
                  }}
                >
                  <div>
                    <span className="pri">{ds.name}</span>
                    <div className="mono dim" style={{ fontSize: 10.5, marginTop: 3 }}>
                      {ds.bucket ? `s3://${ds.bucket}/${ds.prefix ?? ""}` : "—"}
                    </div>
                  </div>
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <Chip tone={resourceTone(ds.status)}>{ds.status}</Chip>
                    <Btn
                      disabled={!available || syncing === ds.ds_id || running}
                      title={
                        !available
                          ? t("knowledge.detail.sources.syncNotReady")
                          : running
                            ? t("knowledge.detail.sources.syncRunning")
                            : undefined
                      }
                      onClick={() => void syncSource(ds)}
                      data-testid="kb-sync-btn"
                    >
                      {syncing === ds.ds_id
                        ? t("knowledge.detail.sources.syncing")
                        : t("knowledge.detail.sources.syncNow")}
                    </Btn>
                    <Btn
                      style={{ color: "var(--crit)", borderColor: "var(--crit)" }}
                      onClick={() => setConfirmDeleteDs(ds)}
                      data-testid="kb-ds-delete-btn"
                    >
                      {t("knowledge.detail.sources.removeSource")}
                    </Btn>
                  </div>
                </div>

                {ds.failure_reasons && ds.failure_reasons.length > 0 && (
                  <div
                    className="note"
                    style={{ marginTop: 10, color: "var(--crit)", borderColor: "var(--crit)" }}
                  >
                    <span className="i">!</span>
                    <span>{ds.failure_reasons.join("; ")}</span>
                  </div>
                )}

                <SourceDocuments kbId={kbId} dsId={ds.ds_id} />

                {jobs.length > 0 && (
                  <div style={{ marginTop: 12 }}>
                    <h4
                      style={{
                        fontFamily: "var(--mono)",
                        fontSize: 9.5,
                        letterSpacing: ".18em",
                        color: "var(--ink-3)",
                        marginBottom: 8,
                        fontWeight: 500,
                      }}
                    >
                      {t("knowledge.detail.sources.jobsTitle")}
                    </h4>
                    {jobs.map((job: IngestionJob) => (
                      <div
                        key={job.job_id}
                        style={{ padding: "7px 0", borderTop: "1px solid var(--grid)" }}
                      >
                        <div
                          style={{
                            display: "flex",
                            justifyContent: "space-between",
                            gap: 10,
                            alignItems: "center",
                          }}
                        >
                          <Chip tone={resourceTone(job.status)}>{job.status}</Chip>
                          <span className="mono dim" style={{ fontSize: 10.5 }}>
                            {job.started_at?.slice(0, 19) ?? "—"}
                          </span>
                        </div>
                        {job.statistics && Object.keys(job.statistics).length > 0 && (
                          <div style={{ marginTop: 6 }}>
                            {Object.entries(job.statistics).map(([k, v]) => (
                              <div className="kv" key={k}>
                                <span className="k">{humanizeStat(k)}</span>
                                <span className="v">{v}</span>
                              </div>
                            ))}
                          </div>
                        )}
                        {job.failure_reasons && job.failure_reasons.length > 0 && (
                          <div
                            className="note"
                            style={{
                              marginTop: 8,
                              color: "var(--crit)",
                              borderColor: "var(--crit)",
                            }}
                          >
                            <span className="i">!</span>
                            <span>{job.failure_reasons.join("; ")}</span>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })
        )}
      </Panel>

      {/* ── Playground ───────────────────────────────────────────────────── */}
      <Panel
        brk
        title={t("knowledge.detail.playground.title")}
        sub={t("knowledge.detail.playground.sub")}
        style={{ marginTop: 14, "--i": 3 } as CSSProperties}
      >
        <div style={{ display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap" }}>
          <div className="field" style={{ flex: 1, minWidth: 260, marginBottom: 0 }}>
            <label htmlFor="kb-query">{t("knowledge.detail.playground.query")}</label>
            <input
              id="kb-query"
              className="input"
              value={queryText}
              onChange={(e) => setQueryText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void runQuery()}
              placeholder={t("knowledge.detail.playground.queryPlaceholder")}
              data-testid="kb-query-input"
            />
          </div>
          <div className="field" style={{ width: 130, marginBottom: 0 }}>
            <label htmlFor="kb-nresults">{t("knowledge.detail.playground.numResults")}</label>
            <input
              id="kb-nresults"
              type="number"
              min={1}
              max={100}
              className="input mono"
              value={numResults}
              onChange={(e) => setNumResults(Math.max(1, Math.min(100, Number(e.target.value) || 1)))}
              data-testid="kb-nresults-input"
            />
          </div>
          <Btn
            primary
            disabled={querying || !queryText.trim()}
            onClick={() => void runQuery()}
            data-testid="kb-query-btn"
          >
            {querying ? t("knowledge.detail.playground.searching") : t("knowledge.detail.playground.search")}
          </Btn>
        </div>

        {queryError && (
          <div
            className="note"
            style={{ marginTop: 12, color: "var(--crit)", borderColor: "var(--crit)" }}
            data-testid="kb-query-error"
          >
            <span className="i">!</span>
            <span>{queryError}</span>
          </div>
        )}

        {results && results.length === 0 && (
          <div className="note" style={{ marginTop: 12 }} data-testid="kb-query-empty">
            <span className="i">[i]</span>
            <span>{t("knowledge.detail.playground.noResults")}</span>
          </div>
        )}

        {results && results.length > 0 && (
          <div style={{ marginTop: 12 }} data-testid="kb-query-results">
            {results.map((r, i) => {
              const isOpen = expanded.has(i);
              const clamp = r.text.length > 320 && !isOpen;
              const shown = clamp ? r.text.slice(0, 320) + "…" : r.text;
              const meta = Object.entries(r.metadata ?? {});
              return (
                <div
                  key={i}
                  style={{ border: "1px solid var(--grid)", padding: 13, marginBottom: 10 }}
                  data-testid="kb-result-card"
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      gap: 10,
                      alignItems: "center",
                      marginBottom: 8,
                    }}
                  >
                    <span className="mono dim" style={{ fontSize: 10.5, wordBreak: "break-all" }}>
                      {r.location_uri || "—"}
                    </span>
                    {typeof r.score === "number" && (
                      <Chip tone="aqua">
                        {t("knowledge.detail.playground.score")} {r.score.toFixed(3)}
                      </Chip>
                    )}
                  </div>
                  <p style={{ fontSize: 12.5, color: "var(--ink-2)", lineHeight: 1.65, margin: 0 }}>
                    {shown}
                  </p>
                  {r.text.length > 320 && (
                    <button
                      type="button"
                      onClick={() => toggleExpanded(i)}
                      style={{
                        background: "none",
                        border: 0,
                        color: "var(--amber)",
                        cursor: "pointer",
                        fontFamily: "var(--mono)",
                        fontSize: 10,
                        marginTop: 6,
                        padding: 0,
                      }}
                    >
                      {isOpen
                        ? t("knowledge.detail.playground.showLess")
                        : t("knowledge.detail.playground.showMore")}
                    </button>
                  )}
                  {meta.length > 0 && (
                    <div style={{ marginTop: 9 }}>
                      {meta.map(([k, v]) => (
                        <div className="kv" key={k}>
                          <span className="k">{k}</span>
                          <span className="v">{typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Panel>

      <ConfirmDialog
        open={confirmDelete}
        title={t("knowledge.detail.delete.title")}
        body={t("knowledge.detail.delete.body", { name: detail.name })}
        confirmLabel={t("knowledge.detail.overview.delete")}
        onConfirm={() => void doDelete(false)}
        onCancel={() => setConfirmDelete(false)}
      />
      <ConfirmDialog
        open={conflict !== null}
        title={t("knowledge.detail.delete.conflictTitle")}
        body={t("knowledge.detail.delete.conflictBody", {
          agents: conflict?.agents.join(", ") || "—",
        })}
        confirmLabel={t("knowledge.detail.delete.force")}
        onConfirm={() => {
          setConflict(null);
          void doDelete(true);
        }}
        onCancel={() => setConflict(null)}
      />
      <ConfirmDialog
        open={confirmDeleteDs !== null}
        title={t("knowledge.detail.sources.removeTitle")}
        body={t("knowledge.detail.sources.removeBody", { name: confirmDeleteDs?.name ?? "" })}
        confirmLabel={t("knowledge.detail.sources.removeSource")}
        onConfirm={() => {
          if (confirmDeleteDs) void deleteSource(confirmDeleteDs);
          setConfirmDeleteDs(null);
        }}
        onCancel={() => setConfirmDeleteDs(null)}
      />
    </section>
  );
}
