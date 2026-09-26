import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { api, errorMessage } from "../../../lib/api";
import {
  type AssistantKbDetail,
  type AssistantKbJob,
  KB_COMPLETE_JOBS,
  KB_NAME_RE,
  KB_RUNNING_JOBS,
  kbHasRunningJob,
  kbUploadSource,
} from "../../../lib/assistant";
import { Alert, Button, Descriptions, Drawer, Field, Tag } from "../../ui";

const POLL_INTERVAL = 5000;
const POLL_LIMIT = 180;

function fmtBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export interface KnowledgeBaseCreateProps {
  workspaceId: string;
  /** Notification only, once creation returns an id; upload/indexing may still be pending. */
  onCreated: (kbId: string) => void;
  onClose: () => void;
}

/** A workspace change resets both the form and every in-flight operation's owner. */
export function KnowledgeBaseCreateDrawer(props: KnowledgeBaseCreateProps) {
  return <KnowledgeBaseCreateWindow key={props.workspaceId} {...props} />;
}

/**
 * Create a managed KB from uploaded files without leaving the assistant: create →
 * upload → (one automatic) sync → poll the ingestion job until it settles. The
 * create endpoint has no idempotency token, so a lost create response is never
 * repeated blindly; a failed or ambiguous sync is retried only explicitly.
 */
function KnowledgeBaseCreateWindow({ workspaceId, onCreated, onClose }: KnowledgeBaseCreateProps) {
  const { t } = useTranslation();
  const fileInput = useRef<HTMLInputElement>(null);
  const lifetime = useRef({ active: true });
  const mutationBusy = useRef(false);
  const syncBusy = useRef(false);
  const autoSyncAttempted = useRef(false);
  const trackedJob = useRef<AssistantKbJob | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [detail, setDetail] = useState<AssistantKbDetail | null>(null);
  const [busy, setBusy] = useState<"create" | "upload" | "repair" | null>(null);
  const [uploaded, setUploaded] = useState(false);
  const [createUnconfirmed, setCreateUnconfirmed] = useState(false);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [job, setJob] = useState<AssistantKbJob | null>(null);
  const [paused, setPaused] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const kbId = detail?.kb_id;

  useEffect(() => {
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
      const started: AssistantKbJob = { job_id: result.job_id, status: result.status };
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
        const source = kbUploadSource(current);
        const observed = source?.ingestion_jobs?.find((item) => item.job_id === trackedJob.current?.job_id);
        if (observed) {
          trackedJob.current = observed;
          setJob(observed);
        }
        // Upload must finish before the first sync, even if the source is
        // already AVAILABLE. An older completed job cannot cover these files.
        if (
          uploaded && current.status === "ACTIVE" && source?.status === "AVAILABLE"
          && !kbHasRunningJob(source) && !autoSyncAttempted.current
        ) {
          await startSync(kbId, source.ds_id);
        }
        if (cancelled || !owner.active) return;
        const finished = observed && !KB_RUNNING_JOBS.has(observed.status);
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
  const nameValid = KB_NAME_RE.test(name.trim());
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
      if (result.keys.length !== files.length) throw new Error(t("assistantPreparation.kb.uploadIncomplete"));
      setUploaded(true);
      refresh();
    } catch (err) {
      if (!owner.active) return;
      setMutationError(errorMessage(err));
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
      const source = kbUploadSource(current);
      if (current.status !== "ACTIVE" || source?.status !== "AVAILABLE") {
        setSyncError(t("assistantPreparation.kb.sourceNotReady"));
      } else if (kbHasRunningJob(source)) {
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

  const source = detail ? kbUploadSource(detail) : undefined;
  const completed = Boolean(job && KB_COMPLETE_JOBS.has(job.status));
  const failedDocuments = job?.statistics?.numberOfDocumentsFailed;
  const indexed = completed && failedDocuments === 0
    && (job?.statistics?.numberOfDocumentsScanned ?? 0) >= files.length
    && !job?.failure_reasons?.length;
  const ingestionRunning = syncing || Boolean(job && KB_RUNNING_JOBS.has(job.status))
    || Boolean(source && kbHasRunningJob(source));
  const ingestionLabel = !uploaded ? "indexAwaitingUpload"
    : indexed ? "indexed"
      : completed && (failedDocuments ?? 0) > 0 ? "indexPartial"
        : completed ? "indexUnverified"
          : job && ["FAILED", "STOPPED"].includes(job.status) ? "indexFailed"
            : ingestionRunning ? "indexing"
              : syncError ? "indexFailed" : "indexWaiting";
  const canRetrySync = uploaded && detail?.status === "ACTIVE" && source?.status === "AVAILABLE"
    && !ingestionRunning && !busy && !indexed && autoSyncAttempted.current;
  const detailUrl = kbId ? `/knowledge-bases?view=detail&kb=${encodeURIComponent(kbId)}` : "/v2/knowledge-bases";
  const reasons = [...(detail?.failure_reasons ?? []), ...(source?.failure_reasons ?? []), ...(job?.failure_reasons ?? [])];

  return (
    <Drawer
      open
      title={t("assistantPreparation.kb.title")}
      onClose={onClose}
      testId="v2-assistant-kb-create"
      footer={
        <>
          <span className="v2-muted" style={{ marginRight: "auto", fontSize: 12.5 }}>{t("assistantPreparation.kb.closeHint")}</span>
          <Button onClick={onClose}>{t("assistantPreparation.kb.close")}</Button>
          {kbId && <Button onClick={refresh} disabled={syncing}>{t("assistantPreparation.kb.refresh")}</Button>}
          {canRetrySync && (
            <Button onClick={() => void retrySync()} testId="v2-assistant-kb-retry-sync">
              {t("assistantPreparation.kb.retrySync")}
            </Button>
          )}
          {!uploaded && !createUnconfirmed && (
            <Button
              kind="primary"
              disabled={busy !== null || Boolean(disabledReason)}
              title={busy ? undefined : disabledReason}
              onClick={() => void submit()}
              testId="v2-assistant-kb-submit"
            >
              {t(`assistantPreparation.kb.${busy === "create" ? "creating"
                : busy === "upload" ? "uploading" : kbId ? "retryUpload" : "create"}`)}
            </Button>
          )}
        </>
      }
    >
      <Alert>{t("assistantPreparation.kb.intro")}</Alert>
      <div className="v2-form">
        <Field label={t("assistantPreparation.kb.name")} required hint={t("assistantPreparation.kb.nameHint")}>
          <input className="v2-input mono" value={name} maxLength={100} disabled={locked}
            onChange={(e) => setName(e.target.value)} data-testid="v2-assistant-kb-name" />
        </Field>
        <Field label={t("assistantPreparation.kb.description")}>
          <textarea className="v2-textarea" value={description} maxLength={1000} disabled={locked}
            onChange={(e) => setDescription(e.target.value)} style={{ minHeight: 64 }} />
        </Field>
        <Field label={t("assistantPreparation.kb.files")} required hint={t("assistantPreparation.kb.filesHint")}>
          <div
            className="v2-assistant-drop"
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => { e.preventDefault(); addFiles(Array.from(e.dataTransfer.files)); }}
          >
            <input ref={fileInput} type="file" multiple disabled={locked} style={{ display: "none" }}
              data-testid="v2-assistant-kb-files"
              onChange={(e) => { addFiles(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
            <Button size="sm" disabled={locked} onClick={() => fileInput.current?.click()}>
              {t("assistantPreparation.kb.chooseFiles")}
            </Button>
            {files.length > 0 && (
              <ul className="v2-assistant-files">
                {files.map((file, index) => (
                  <li key={`${file.name}:${file.size}:${file.lastModified}`}>
                    <span>{file.name}</span>
                    <span className="v2-muted">{fmtBytes(file.size)}</span>
                    {!locked && (
                      <button type="button" className="v2-link danger"
                        aria-label={t("assistantPreparation.kb.removeFile", { name: file.name })}
                        onClick={() => setFiles((current) => current.filter((_, i) => i !== index))}>
                        ×
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </Field>
      </div>
      {detail && (
        <div role="status" aria-live="polite" data-testid="v2-assistant-kb-progress" style={{ marginTop: 16 }}>
          <Descriptions
            one
            items={[
              { label: "KB", value: <span className="mono">{t("assistantPreparation.kb.created", { id: detail.kb_id })}</span> },
              {
                label: t("v2.assistant.kbResource"),
                value: (
                  <span className="v2-stack" style={{ gap: 2 }}>
                    <span>{t("assistantPreparation.kb.resourceStatus", { status: detail.status })}</span>
                    <span className="v2-muted">
                      {t(`assistantPreparation.kb.${detail.status === "ACTIVE" ? "mountable" : "notMountable"}`)}
                    </span>
                  </span>
                ),
              },
              {
                label: t("v2.assistant.kbUpload"),
                value: t(`assistantPreparation.kb.${uploaded ? "uploaded" : busy === "upload" ? "uploading" : "uploadPending"}`,
                  { count: files.length }),
              },
              {
                label: t("v2.assistant.kbIndex"),
                value: (
                  <span className="v2-stack" style={{ gap: 2 }} data-testid="v2-assistant-kb-ingestion">
                    <Tag tone={indexed ? "green" : ingestionLabel === "indexFailed" ? "red" : ingestionRunning ? "blue" : "gray"}>
                      {t(`assistantPreparation.kb.${ingestionLabel}`, { count: failedDocuments ?? 0 })}
                    </Tag>
                    {source && <span className="v2-muted">{t("assistantPreparation.kb.sourceStatus", { status: source.status })}</span>}
                  </span>
                ),
              },
            ]}
          />
          {reasons.map((reason, index) => (
            <div key={`${index}:${reason}`} style={{ marginTop: 8 }}><Alert tone="error">{reason}</Alert></div>
          ))}
        </div>
      )}
      <div style={{ marginTop: 12 }}>
        {mutationError && (
          <Alert tone="error">
            {t(`assistantPreparation.kb.${createUnconfirmed ? "createUnconfirmed"
              : kbId && !uploaded ? "uploadFailed" : "operationFailed"}`, { message: mutationError })}
          </Alert>
        )}
        {loadError && <Alert tone="error">{t("assistantPreparation.kb.loadFailed", { message: loadError })}</Alert>}
        {syncError && <Alert tone="error">{t("assistantPreparation.kb.syncFailed", { message: syncError })}</Alert>}
        {paused && <Alert tone="warn">{t("assistantPreparation.kb.pollPaused")}</Alert>}
        {detail?.status === "ACTIVE" && detail.data_sources.length === 0 && (
          <Alert
            tone="warn"
            action={
              <Button size="sm" disabled={busy !== null} onClick={() => void repairSource()}>
                {t(`assistantPreparation.kb.${busy === "repair" ? "repairing" : "repairSource"}`)}
              </Button>
            }
          >
            {t("assistantPreparation.kb.sourcePending")}
          </Alert>
        )}
        {(kbId || createUnconfirmed) && (
          <Link to={detailUrl} className="v2-link">
            {t(`assistantPreparation.kb.${kbId ? "openDetail" : "openCatalog"}`)}
          </Link>
        )}
      </div>
    </Drawer>
  );
}
