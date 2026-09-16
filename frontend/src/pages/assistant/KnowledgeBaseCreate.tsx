import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { Btn } from "../../components/Btn";
import { Panel } from "../../components/Panel";
import { api, errorMessage } from "../../lib/api";
import type { DataSource, IngestionJob, KnowledgeBaseDetail } from "../KnowledgeBases";
import { formatBytes } from "../knowledge/kb-helpers";

export interface KnowledgeBaseCreateProps {
  workspaceId: string;
  /** Notification only, once creation returns an id; upload/indexing may still be pending. */
  onCreated: (kbId: string) => void;
  onClose: () => void;
}

const NAME_RE = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}$/;
const RUNNING_JOBS = new Set(["STARTING", "IN_PROGRESS", "STOPPING"]);
const COMPLETE_JOBS = new Set(["COMPLETE", "COMPLETED"]);
const POLL_INTERVAL = 5000;
const POLL_LIMIT = 180;

function hasRunningJob(source: DataSource): boolean {
  return (source.ingestion_jobs ?? []).some((job) => RUNNING_JOBS.has(job.status));
}

function uploadSource(detail: KnowledgeBaseDetail): DataSource | undefined {
  return detail.data_sources.find((source) => source.prefix === `kb/${detail.kb_id}/`)
    ?? (detail.data_sources.length === 1 ? detail.data_sources[0] : undefined);
}

/** A workspace change resets both the form and every in-flight operation's owner. */
export function KnowledgeBaseCreate(props: KnowledgeBaseCreateProps) {
  return <KnowledgeBaseCreateWindow key={props.workspaceId} {...props} />;
}

function KnowledgeBaseCreateWindow({
  workspaceId, onCreated, onClose,
}: KnowledgeBaseCreateProps) {
  const { t } = useTranslation();
  const fieldId = useId();
  const fileInput = useRef<HTMLInputElement>(null);
  const lifetime = useRef({ active: true });
  const mutationBusy = useRef(false);
  const syncBusy = useRef(false);
  const autoSyncAttempted = useRef(false);
  const trackedJob = useRef<IngestionJob | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [detail, setDetail] = useState<KnowledgeBaseDetail | null>(null);
  const [busy, setBusy] = useState<"create" | "upload" | "repair" | null>(null);
  const [uploaded, setUploaded] = useState(false);
  const [createUnconfirmed, setCreateUnconfirmed] = useState(false);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [job, setJob] = useState<IngestionJob | null>(null);
  const [paused, setPaused] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const kbId = detail?.kb_id;

  useEffect(() => {
    // A fresh object also isolates the first effect's cleanup in StrictMode.
    const owner = { active: true };
    lifetime.current = owner;
    return () => { owner.active = false; };
  }, []);

  const refresh = () => {
    setPaused(false);
    setRefreshKey((key) => key + 1);
  };

  const startSync = useCallback(async (id: string, sourceId: string) => {
    if (syncBusy.current || !lifetime.current.active) return;
    const owner = lifetime.current;
    syncBusy.current = true;
    // Set before the request: failed/ambiguous responses must never cause an
    // automatic POST on every subsequent poll. A retry is an explicit action.
    autoSyncAttempted.current = true;
    setSyncing(true);
    setSyncError(null);
    trackedJob.current = null;
    setJob(null);
    try {
      const result = await api.syncKnowledgeBase(id, sourceId, workspaceId);
      if (!owner.active) return;
      if (typeof result.job_id !== "string" || typeof result.status !== "string") {
        throw new Error(t("assistantPreparation.kb.invalidSyncResponse"));
      }
      const started: IngestionJob = { job_id: result.job_id, status: result.status };
      trackedJob.current = started;
      setJob(started);
    } catch (err) {
      if (owner.active) setSyncError(errorMessage(err));
    } finally {
      if (owner.active) {
        syncBusy.current = false;
        setSyncing(false);
      }
    }
  }, [workspaceId, t]);

  useEffect(() => {
    if (!kbId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let polls = 0;
    const owner = lifetime.current;
    const tick = async () => {
      try {
        const current = await api.getKnowledgeBase(kbId, workspaceId);
        if (cancelled || !owner.active) return;
        setDetail(current);
        setLoadError(null);
        const source = uploadSource(current);
        const observed = source?.ingestion_jobs?.find(
          (item) => item.job_id === trackedJob.current?.job_id,
        );
        if (observed) {
          trackedJob.current = observed;
          setJob(observed);
        }
        // Upload must finish before the first sync, even if the source is
        // already AVAILABLE. An older completed job cannot cover these files.
        if (
          uploaded && current.status === "ACTIVE" && source?.status === "AVAILABLE"
          && !hasRunningJob(source) && !autoSyncAttempted.current
        ) {
          await startSync(kbId, source.ds_id);
        }
        if (cancelled || !owner.active) return;
        // Even an immediately completed POST needs a detail read to obtain
        // document statistics before the window can report indexing success.
        const finished = observed && !RUNNING_JOBS.has(observed.status);
        if (["FAILED", "DELETING"].includes(current.status) || finished) return;
        if (++polls >= POLL_LIMIT) {
          setPaused(true);
          return;
        }
        timer = setTimeout(() => void tick(), POLL_INTERVAL);
      } catch (err) {
        if (!cancelled && owner.active) {
          setLoadError(errorMessage(err));
          setPaused(true);
        }
      }
    };
    void tick();
    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [kbId, workspaceId, uploaded, refreshKey, startSync]);

  const locked = Boolean(kbId) || busy !== null || createUnconfirmed;
  const nameValid = NAME_RE.test(name.trim());
  // S3 stores basenames; different files with the same destination overwrite.
  const destinations = files.map((file) => file.name.replace(/\\/g, "/").split("/").pop()?.trim());
  const duplicateNames = new Set(destinations).size !== destinations.length;
  const disabledReason = !nameValid ? t("assistantPreparation.kb.nameHint")
    : files.length === 0 ? t("assistantPreparation.kb.filesRequired")
    : duplicateNames ? t("assistantPreparation.kb.duplicateNames") : undefined;

  const addFiles = (picked: File[]) => {
    if (locked) return;
    setFiles((previous) => {
      const next = [...previous];
      for (const file of picked) {
        if (!next.some((item) => item.name === file.name && item.size === file.size
          && item.lastModified === file.lastModified)) next.push(file);
      }
      return next;
    });
  };

  const submit = async () => {
    if (mutationBusy.current || disabledReason || uploaded || createUnconfirmed) return;
    const owner = lifetime.current;
    mutationBusy.current = true;
    setMutationError(null);
    let id = kbId;
    try {
      if (!id) {
        setBusy("create");
        const created = await api.createKnowledgeBase({
          name: name.trim(), description: description.trim(), source: { mode: "upload" },
        }, workspaceId);
        if (!owner.active) return;
        id = created.kb_id;
        if (!id) throw new Error(t("assistantPreparation.kb.invalidCreateResponse"));
        setDetail(created);
        onCreated(id);
      }
      if (!owner.active) return;
      setBusy("upload");
      const result = await api.uploadKnowledgeBaseFiles(id, files, workspaceId);
      if (!owner.active) return;
      if (result.keys.length !== files.length) {
        throw new Error(t("assistantPreparation.kb.uploadIncomplete"));
      }
      setUploaded(true);
      refresh();
    } catch (err) {
      if (!owner.active) return;
      setMutationError(errorMessage(err));
      // The create endpoint has no idempotency token. If its response is lost,
      // do not blindly repeat creation and potentially leave a duplicate KB.
      if (!id) setCreateUnconfirmed(true);
    } finally {
      if (owner.active) {
        mutationBusy.current = false;
        setBusy(null);
      }
    }
  };

  const repairSource = async () => {
    if (!kbId || mutationBusy.current) return;
    const owner = lifetime.current;
    mutationBusy.current = true;
    setBusy("repair");
    setMutationError(null);
    try {
      await api.addKnowledgeBaseSource(kbId, { mode: "upload" }, workspaceId);
      if (owner.active) refresh();
    } catch (err) {
      if (owner.active) setMutationError(errorMessage(err));
    } finally {
      if (owner.active) {
        mutationBusy.current = false;
        setBusy(null);
      }
    }
  };

  const retrySync = async () => {
    if (!kbId || mutationBusy.current || syncBusy.current) return;
    const owner = lifetime.current;
    mutationBusy.current = true;
    setSyncError(null);
    try {
      // Re-read before a manual retry, including after an ambiguous POST error.
      const current = await api.getKnowledgeBase(kbId, workspaceId);
      if (!owner.active) return;
      setDetail(current);
      setLoadError(null);
      const source = uploadSource(current);
      if (current.status !== "ACTIVE" || source?.status !== "AVAILABLE") {
        setSyncError(t("assistantPreparation.kb.sourceNotReady"));
      } else if (hasRunningJob(source)) {
        setSyncError(t("assistantPreparation.kb.syncAlreadyRunning"));
      } else {
        await startSync(kbId, source.ds_id);
      }
      if (owner.active) refresh();
    } catch (err) {
      if (owner.active) setSyncError(errorMessage(err));
    } finally {
      if (owner.active) mutationBusy.current = false;
    }
  };

  const source = detail ? uploadSource(detail) : undefined;
  const completed = Boolean(job && COMPLETE_JOBS.has(job.status));
  const failedDocuments = job?.statistics?.numberOfDocumentsFailed;
  const indexed = completed && failedDocuments === 0
    && (job?.statistics?.numberOfDocumentsScanned ?? 0) >= files.length
    && !job?.failure_reasons?.length;
  const ingestionRunning = syncing || Boolean(job && RUNNING_JOBS.has(job.status))
    || Boolean(source && hasRunningJob(source));
  const ingestionLabel = !uploaded ? "indexAwaitingUpload"
    : indexed ? "indexed"
    : completed && (failedDocuments ?? 0) > 0 ? "indexPartial"
    : completed ? "indexUnverified"
    : job && ["FAILED", "STOPPED"].includes(job.status) ? "indexFailed"
    : ingestionRunning ? "indexing"
    : syncError ? "indexFailed" : "indexWaiting";
  const canRetrySync = uploaded && detail?.status === "ACTIVE" && source?.status === "AVAILABLE"
    && !ingestionRunning && !busy && !indexed && autoSyncAttempted.current;
  const detailUrl = kbId ? `/knowledge-bases?view=detail&kb=${encodeURIComponent(kbId)}`
    : "/knowledge-bases";

  return (
    <Panel
      title={t("assistantPreparation.kb.title")}
      role="region"
      aria-label={t("assistantPreparation.kb.title")}
      data-testid="assistant-kb-create"
      style={{ minWidth: 0, overflowWrap: "anywhere" }}
      end={<Btn type="button" onClick={onClose}>{t("assistantPreparation.kb.close")}</Btn>}
    >
      <p className="dim" style={{ marginTop: 0 }}>{t("assistantPreparation.kb.intro")}</p>
      <div className="field">
        <label htmlFor={`${fieldId}-name`}>{t("assistantPreparation.kb.name")}</label>
        <input
          id={`${fieldId}-name`} className="input mono" value={name} maxLength={100}
          disabled={locked} onChange={(event) => setName(event.target.value)}
          aria-describedby={`${fieldId}-name-hint`} data-testid="assistant-kb-name"
        />
        <div id={`${fieldId}-name-hint`} className="dim" style={{ marginTop: 6 }}>
          {t("assistantPreparation.kb.nameHint")}
        </div>
      </div>
      <div className="field">
        <label htmlFor={`${fieldId}-description`}>{t("assistantPreparation.kb.description")}</label>
        <textarea
          id={`${fieldId}-description`} className="input" value={description} maxLength={1000}
          disabled={locked} onChange={(event) => setDescription(event.target.value)}
          style={{ minHeight: 64, resize: "vertical" }}
        />
      </div>
      <div
        className="field"
        onDragOver={(event) => { event.preventDefault(); }}
        onDrop={(event) => { event.preventDefault(); addFiles(Array.from(event.dataTransfer.files)); }}
        style={{ border: "1px dashed var(--line)", padding: 12, minWidth: 0 }}
      >
        <label htmlFor={`${fieldId}-files`}>{t("assistantPreparation.kb.files")}</label>
        <input
          ref={fileInput} id={`${fieldId}-files`} type="file" multiple disabled={locked}
          style={{ display: "none" }} data-testid="assistant-kb-files"
          onChange={(event) => {
            addFiles(Array.from(event.target.files ?? []));
            event.target.value = "";
          }}
        />
        <Btn type="button" disabled={locked} onClick={() => fileInput.current?.click()}>
          {t("assistantPreparation.kb.chooseFiles")}
        </Btn>
        <p className="dim">{t("assistantPreparation.kb.filesHint")}</p>
        {files.length > 0 && (
          <ul style={{ listStyle: "none", padding: 0, marginBottom: 0, maxHeight: 160, overflowY: "auto" }}>
            {files.map((file, index) => (
              <li
                key={`${file.name}:${file.size}:${file.lastModified}`}
                style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 6 }}
              >
                <span style={{ flex: 1, minWidth: 0, overflowWrap: "anywhere" }}>{file.name}</span>
                <span className="dim" style={{ whiteSpace: "nowrap" }}>{formatBytes(file.size)}</span>
                {!locked && (
                  <Btn
                    type="button" aria-label={t("assistantPreparation.kb.removeFile", { name: file.name })}
                    onClick={() => setFiles((current) => current.filter((_, i) => i !== index))}
                  >×</Btn>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
      {detail && (
        <div role="status" aria-live="polite" data-testid="assistant-kb-progress">
          <p className="mono" style={{ overflowWrap: "anywhere" }}>
            {t("assistantPreparation.kb.created", { id: detail.kb_id })}
          </p>
          <p>{t("assistantPreparation.kb.resourceStatus", { status: detail.status })}</p>
          <p>{t(`assistantPreparation.kb.${detail.status === "ACTIVE" ? "mountable" : "notMountable"}`)}</p>
          <p>{t(`assistantPreparation.kb.${uploaded ? "uploaded" : busy === "upload" ? "uploading" : "uploadPending"}`,
            { count: files.length })}</p>
          <p data-testid="assistant-kb-ingestion" style={{ color: indexed ? "var(--good)" : undefined }}>
            {t(`assistantPreparation.kb.${ingestionLabel}`, { count: failedDocuments ?? 0 })}
          </p>
          {source && <p className="dim">{t("assistantPreparation.kb.sourceStatus", { status: source.status })}</p>}
          {[...(detail.failure_reasons ?? []), ...(source?.failure_reasons ?? []),
            ...(job?.failure_reasons ?? [])].map((reason, index) => (
            <p key={`${index}:${reason}`} style={{ color: "var(--crit)", overflowWrap: "anywhere" }}>{reason}</p>
          ))}
        </div>
      )}
      {mutationError && (
        <div role="alert" className="note" style={{ color: "var(--crit)", overflowWrap: "anywhere" }}>
          {t(`assistantPreparation.kb.${createUnconfirmed ? "createUnconfirmed"
            : kbId && !uploaded ? "uploadFailed" : "operationFailed"}`, { message: mutationError })}
        </div>
      )}
      {loadError && <p role="alert">{t("assistantPreparation.kb.loadFailed", { message: loadError })}</p>}
      {syncError && <p role="alert">{t("assistantPreparation.kb.syncFailed", { message: syncError })}</p>}
      {paused && <p className="dim">{t("assistantPreparation.kb.pollPaused")}</p>}
      {detail?.status === "ACTIVE" && detail.data_sources.length === 0 && (
        <div className="note" style={{ display: "block" }}>
          <p style={{ marginTop: 0 }}>{t("assistantPreparation.kb.sourcePending")}</p>
          <Btn type="button" disabled={busy !== null} onClick={() => void repairSource()}>
            {t(`assistantPreparation.kb.${busy === "repair" ? "repairing" : "repairSource"}`)}
          </Btn>
        </div>
      )}
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8, marginTop: 12 }}>
        {!uploaded && !createUnconfirmed && (
          <Btn
            primary type="button" disabled={busy !== null || Boolean(disabledReason)}
            disabledReason={busy ? undefined : disabledReason}
            onClick={() => void submit()} data-testid="assistant-kb-submit"
            style={{ maxWidth: "100%", whiteSpace: "normal", textAlign: "left" }}
          >
            {t(`assistantPreparation.kb.${busy === "create" ? "creating"
              : busy === "upload" ? "uploading" : kbId ? "retryUpload" : "create"}`)}
          </Btn>
        )}
        {canRetrySync && (
          <Btn type="button" onClick={() => void retrySync()} data-testid="assistant-kb-retry-sync">
            {t("assistantPreparation.kb.retrySync")}
          </Btn>
        )}
        {kbId && (
          <Btn type="button" onClick={refresh} disabled={syncing}>
            {t("assistantPreparation.kb.refresh")}
          </Btn>
        )}
        {(kbId || createUnconfirmed) && (
          <Link to={detailUrl} style={{ color: "var(--amber)", overflowWrap: "anywhere" }}>
            {t(`assistantPreparation.kb.${kbId ? "openDetail" : "openCatalog"}`)}
          </Link>
        )}
      </div>
      <p className="dim" style={{ marginBottom: 0 }}>{t("assistantPreparation.kb.closeHint")}</p>
    </Panel>
  );
}
