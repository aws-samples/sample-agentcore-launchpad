import { Plus } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { api, ApiError, errorMessage, v2KnowledgeApi } from "../../../lib/api";
import {
  type DataSource,
  jobRunning,
  KB_DESCRIPTION_MAX,
  kbInFlight,
  type KnowledgeBaseDetail,
} from "../../../lib/knowledgeBases";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Confirm,
  Descriptions,
  FlowHeader,
  LinkButton,
  Modal,
  Spin,
  SubTabs,
  Table,
  Tag,
} from "../../ui";
import { KbStatusTag, ResourceTag } from "./common";
import { SourcePanel } from "./KbDocuments";
import { KbRetrieve } from "./KbRetrieve";
import { emptySource, sourceBody, type SourceDraft } from "./source";
import { SourceFields } from "./SourceFields";
import { useKbDelete } from "./useKbDelete";

const POLL_MS = 5000;
/** ~3.5 min at the 5 s poll: how long an ACTIVE KB without a source waits for the
 *  backend completion thread before only the manual repair is offered. */
const SOURCE_WAIT_TICKS = 40;
/** Codes meaning the linked id does not resolve in this workspace. */
const GONE_CODES = new Set(["kb.not_found", "aws.not_found", "aws.validation", "aws.access_denied", "http.404"]);

type Tab = "sources" | "retrieve";

function AddSourceModal({
  kbId,
  open,
  onClose,
  onDone,
}: {
  kbId: string;
  open: boolean;
  onClose: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [draft, setDraft] = useState<SourceDraft>(emptySource);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldError, setFieldError] = useState<string | null>(null);

  const submit = async () => {
    setError(null);
    if (draft.mode === "upload" && draft.files.length === 0) {
      setFieldError(t("knowledge.detail.sources.uploadEmpty"));
      return;
    }
    if (draft.mode === "existing" && !draft.bucket.trim()) {
      setFieldError(t("knowledge.detail.sources.bucketEmpty"));
      return;
    }
    setFieldError(null);
    setBusy(true);
    try {
      if (draft.mode === "upload") {
        // files land in the KB's artifacts-bucket prefix, i.e. its upload source
        await v2KnowledgeApi.uploadFiles(kbId, draft.files);
        toast("success", t("knowledge.detail.sources.uploadedFiles", { n: draft.files.length }));
      } else {
        await v2KnowledgeApi.addSource(kbId, sourceBody(draft));
        toast("success", t("knowledge.detail.sources.added"));
      }
      setDraft(emptySource());
      onDone();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      wide
      title={t("knowledge.detail.sources.add")}
      onClose={onClose}
      testId="v2-kb-add-source"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          <Button kind="primary" disabled={busy} onClick={() => void submit()} testId="v2-kb-add-submit">
            {busy
              ? t("knowledge.detail.sources.adding")
              : draft.mode === "upload"
                ? t("knowledge.detail.sources.uploadSubmit")
                : t("knowledge.detail.sources.addSubmit")}
          </Button>
        </>
      }
    >
      {error && <Alert tone="error">{error}</Alert>}
      <SourceFields
        value={draft}
        onChange={(next) => {
          if (next.mode !== draft.mode) setFieldError(null);
          setDraft(next);
        }}
        error={fieldError}
      />
    </Modal>
  );
}

function DescriptionModal({
  kb,
  open,
  onClose,
  onSaved,
}: {
  kb: KnowledgeBaseDetail;
  open: boolean;
  onClose: () => void;
  onSaved: (description: string) => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [draft, setDraft] = useState(kb.description);
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true);
    try {
      await v2KnowledgeApi.updateDescription(kb.kb_id, draft);
      toast("success", t("v2.knowledge.descSaved"));
      onSaved(draft);
    } catch (err) {
      toast("error", t("common.actionFailed", { msg: errorMessage(err) }));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      title={t("v2.knowledge.editDesc")}
      onClose={onClose}
      testId="v2-kb-desc-modal"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          <Button kind="primary" disabled={busy} onClick={() => void save()} testId="v2-kb-desc-save">
            {busy ? t("knowledge.detail.overview.saving") : t("v2.common.save")}
          </Button>
        </>
      }
    >
      <textarea
        className="v2-textarea"
        style={{ minHeight: 140 }}
        value={draft}
        maxLength={KB_DESCRIPTION_MAX}
        onChange={(e) => setDraft(e.target.value)}
        placeholder={t("knowledge.create.descriptionPlaceholder")}
        data-testid="v2-kb-desc-input"
      />
      <p className="v2-muted v2-knowledge-hint">
        {t("knowledge.create.descriptionHint")} ({draft.length}/{KB_DESCRIPTION_MAX})
      </p>
    </Modal>
  );
}

export function KbDetail({ id, onGone }: { id: string; onGone: (id: string) => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [params, setParams] = useSearchParams();
  const tab: Tab = params.get("tab") === "retrieve" ? "retrieve" : "sources";
  const setTab = (next: Tab) =>
    setParams(next === "sources" ? { view: "detail", id } : { view: "detail", id, tab: next }, { replace: true });

  const [kb, setKb] = useState<KnowledgeBaseDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);
  const [selected, setSelected] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);
  const [editDesc, setEditDesc] = useState(false);
  const [syncing, setSyncing] = useState<string | null>(null);
  const [repairing, setRepairing] = useState(false);
  const [removeDs, setRemoveDs] = useState<DataSource | null>(null);
  const [removing, setRemoving] = useState(false);
  const del = useKbDelete(() => setParams({}));

  // attached agents are names; resolve them to V2 agent detail links
  const agents = useLoad(() => api.listAgents(), "agents");
  const agentIds = useMemo(
    () => new Map((agents.data?.agents ?? []).map((a) => [a.name, a.id])),
    [agents.data],
  );

  const onGoneRef = useRef(onGone);
  onGoneRef.current = onGone;
  const loadDetail = useCallback(async (): Promise<KnowledgeBaseDetail | null> => {
    try {
      const fresh = await v2KnowledgeApi.get(id);
      setKb(fresh);
      setError(null);
      return fresh;
    } catch (err) {
      setError(errorMessage(err));
      if (err instanceof ApiError && GONE_CODES.has(err.code)) onGoneRef.current(id);
      return null;
    }
  }, [id]);

  // Create-flow automation guards (each step at most once per mount).
  const autoSynced = useRef<Set<string>>(new Set());
  const sourceWaitTicks = useRef(0);

  // A slow create has its data source finished by a backend thread: keep polling
  // for a bounded window so it appears on its own; past that, offer the repair.
  const awaitingBackendSource = useCallback((d: KnowledgeBaseDetail): boolean => {
    if (String(d.status).toUpperCase() !== "ACTIVE" || d.data_sources.length > 0) {
      sourceWaitTicks.current = 0;
      return false;
    }
    sourceWaitTicks.current += 1;
    return sourceWaitTicks.current <= SOURCE_WAIT_TICKS;
  }, []);

  // The first ingestion starts automatically once a data source is AVAILABLE
  // and has no jobs yet.
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
          try {
            await v2KnowledgeApi.sync(id, ds.ds_id);
            toast("success", t("knowledge.detail.sources.syncStarted"));
          } catch {
            // the manual "sync now" stays available
          }
        }
      }
      return fired;
    },
    [id, t, toast],
  );

  // Poll while anything is in flight; stop once quiescent. A mutation bumps
  // refreshKey, which restarts the loop.
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

  const sources = useMemo(() => kb?.data_sources ?? [], [kb]);
  const current = sources.find((ds) => ds.ds_id === selected) ?? sources[0] ?? null;

  const sync = async (ds: DataSource) => {
    setSyncing(ds.ds_id);
    try {
      await v2KnowledgeApi.sync(id, ds.ds_id);
      toast("success", t("knowledge.detail.sources.syncStarted"));
      refresh();
    } catch (err) {
      toast("error", t("knowledge.detail.sources.syncFailed", { msg: errorMessage(err) }));
    } finally {
      setSyncing(null);
    }
  };

  // Idempotent server-side, so a stray click cannot create a second source.
  const repair = async () => {
    setRepairing(true);
    try {
      await v2KnowledgeApi.addSource(id, { mode: "upload" });
      toast("success", t("knowledge.detail.sources.autoCreated"));
      sourceWaitTicks.current = 0;
      refresh();
    } catch (err) {
      toast("error", t("common.actionFailed", { msg: errorMessage(err) }));
    } finally {
      setRepairing(false);
    }
  };

  const removeSource = async () => {
    if (!removeDs) return;
    setRemoving(true);
    try {
      await v2KnowledgeApi.removeSource(id, removeDs.ds_id);
      toast("success", t("knowledge.detail.sources.deleted"));
      setRemoveDs(null);
      refresh();
    } catch (err) {
      toast("error", t("common.actionFailed", { msg: errorMessage(err) }));
    } finally {
      setRemoving(false);
    }
  };

  if (error && !kb) {
    return (
      <>
        <FlowHeader title={id} onBack={() => setParams({})} />
        <Alert tone="error" action={<LinkButton onClick={refresh}>{t("v2.common.retry")}</LinkButton>}>
          {t("knowledge.detail.loadFailedTitle")}: {error}
        </Alert>
      </>
    );
  }
  if (!kb) return <Spin />;

  const provisioning = kb.status === "CREATING";
  const missingSource = !provisioning && kb.status === "ACTIVE" && sources.length === 0;

  const sourceColumns: Column<DataSource>[] = [
    {
      key: "name",
      title: t("v2.knowledge.colSource"),
      render: (ds) => (
        <>
          <LinkButton onClick={() => setSelected(ds.ds_id)}>{ds.name}</LinkButton>
          <span className="sub mono">{ds.bucket ? `s3://${ds.bucket}/${ds.prefix ?? ""}` : "—"}</span>
        </>
      ),
    },
    {
      key: "status",
      title: t("knowledge.cols.status"),
      render: (ds) => <ResourceTag status={ds.status} title={ds.failure_reasons?.join("; ") || undefined} />,
    },
    {
      key: "job",
      title: t("v2.knowledge.colLastSync"),
      render: (ds) => {
        const job = ds.ingestion_jobs?.[0];
        return job ? (
          <>
            <ResourceTag status={job.status} />
            <span className="sub">{fmtTime(job.started_at)}</span>
          </>
        ) : (
          <span className="v2-muted">—</span>
        );
      },
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (ds) => {
        const available = ds.status.toUpperCase() === "AVAILABLE";
        const running = (ds.ingestion_jobs ?? []).some((j) => jobRunning(j));
        return (
          <div className="v2-actions">
            <LinkButton
              disabled={!available || running || syncing === ds.ds_id}
              title={
                !available
                  ? t("knowledge.detail.sources.syncNotReady")
                  : running
                    ? t("knowledge.detail.sources.syncRunning")
                    : undefined
              }
              onClick={() => void sync(ds)}
              testId="v2-kb-sync"
            >
              {syncing === ds.ds_id ? t("knowledge.detail.sources.syncing") : t("knowledge.detail.sources.syncNow")}
            </LinkButton>
            <LinkButton onClick={() => setSelected(ds.ds_id)}>{t("knowledge.detail.sources.documents")}</LinkButton>
            <LinkButton danger onClick={() => setRemoveDs(ds)} testId="v2-kb-ds-remove">
              {t("knowledge.detail.sources.removeSource")}
            </LinkButton>
          </div>
        );
      },
    },
  ];

  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            {kb.name}
            <KbStatusTag status={kb.status} />
          </span>
        }
        onBack={() => setParams({})}
        end={
          <>
            <Button onClick={refresh}>{t("v2.common.refresh")}</Button>
            <Button kind="danger" disabled={kb.status === "DELETING"} onClick={() => del.ask(kb)} testId="v2-kb-delete">
              {t("v2.common.delete")}
            </Button>
            <Button kind="primary" onClick={() => setShowAdd(true)} testId="v2-kb-add">
              <Plus size={14} aria-hidden="true" />
              {t("knowledge.detail.sources.add")}
            </Button>
          </>
        }
      />
      {error && <Alert tone="warn">{error}</Alert>}
      {kb.failure_reasons && kb.failure_reasons.length > 0 && (
        <Alert tone="error">{kb.failure_reasons.join("; ")}</Alert>
      )}
      {provisioning && <Alert>{t("knowledge.detail.sources.provisioning")}</Alert>}
      {missingSource && (
        <Alert
          tone="warn"
          action={
            <Button size="sm" disabled={repairing} onClick={() => void repair()} testId="v2-kb-repair">
              {repairing ? t("knowledge.detail.sources.adding") : t("knowledge.detail.sources.repairSource")}
            </Button>
          }
        >
          <span data-testid="v2-kb-missing-source">{t("knowledge.detail.sources.missingSource")}</span>
        </Alert>
      )}

      <Card title={t("v2.knowledge.basic")} testId="v2-kb-basic">
        <Descriptions
          items={[
            { label: t("knowledge.cols.name"), value: kb.name },
            { label: t("knowledge.detail.overview.kbId"), value: <span className="mono">{kb.kb_id}</span> },
            { label: t("knowledge.cols.status"), value: <KbStatusTag status={kb.status} /> },
            { label: t("knowledge.cols.dataSources"), value: <span className="mono">{sources.length}</span> },
            { label: t("v2.common.createdAt"), value: fmtTime(kb.created_at) },
            { label: t("knowledge.detail.overview.updated"), value: fmtTime(kb.updated_at) },
            { label: t("knowledge.detail.overview.arn"), value: kb.arn ? <span className="mono">{kb.arn}</span> : "—" },
            {
              label: t("knowledge.detail.overview.description"),
              value: (
                <span className="v2-knowledge-desc">
                  {kb.description || <span className="v2-muted">{t("knowledge.detail.overview.noDescription")}</span>}
                  <LinkButton onClick={() => setEditDesc(true)} testId="v2-kb-desc-edit">
                    {t("v2.common.edit")}
                  </LinkButton>
                </span>
              ),
            },
          ]}
        />
      </Card>

      <Card
        title={t("knowledge.detail.agents.title")}
        sub={t("knowledge.detail.agents.sub")}
        end={
          <Link to="/v2/agents?view=new" className="v2-link">
            {t("v2.knowledge.attachCta")}
          </Link>
        }
        testId="v2-kb-agents"
      >
        {kb.attached_agents.length === 0 ? (
          <span className="v2-muted">{t("knowledge.detail.agents.empty")}</span>
        ) : (
          <div className="v2-tags">
            {kb.attached_agents.map((name) => {
              const agentId = agentIds.get(name);
              return agentId ? (
                <Link key={name} to={`/v2/agents?view=detail&id=${encodeURIComponent(agentId)}`}>
                  <Tag tone="outline">{name}</Tag>
                </Link>
              ) : (
                <Tag key={name} tone="outline">
                  {name}
                </Tag>
              );
            })}
          </div>
        )}
        <p className="v2-muted v2-knowledge-hint">{t("v2.knowledge.attachHint")}</p>
      </Card>

      <div className="v2-knowledge-tabs">
        <SubTabs
          value={tab}
          onChange={setTab}
          tabs={[
            { value: "sources", label: `${t("knowledge.detail.sources.title")} (${sources.length})` },
            { value: "retrieve", label: t("v2.knowledge.retrieveTest") },
          ]}
        />
      </div>

      {tab === "sources" ? (
        <>
          <Card title={t("knowledge.detail.sources.title")} sub={t("knowledge.detail.sources.sub")} testId="v2-kb-sources">
            <Table
              columns={sourceColumns}
              rows={sources}
              rowKey={(ds) => ds.ds_id}
              selectedKey={current?.ds_id ?? null}
              empty={provisioning ? t("knowledge.detail.sources.provisioning") : t("knowledge.detail.sources.empty")}
              testId="v2-kb-sources-table"
            />
          </Card>
          {current && (
            <Card
              title={current.name}
              sub={current.bucket ? `s3://${current.bucket}/${current.prefix ?? ""}` : undefined}
              testId="v2-kb-source-panel"
            >
              <SourcePanel key={current.ds_id} kbId={kb.kb_id} source={current} />
            </Card>
          )}
        </>
      ) : (
        <KbRetrieve kbId={kb.kb_id} active={kb.status === "ACTIVE"} />
      )}

      <AddSourceModal
        kbId={kb.kb_id}
        open={showAdd}
        onClose={() => setShowAdd(false)}
        onDone={() => {
          setShowAdd(false);
          refresh();
        }}
      />
      {editDesc && (
        <DescriptionModal
          kb={kb}
          open
          onClose={() => setEditDesc(false)}
          onSaved={(description) => {
            setKb((prev) => (prev ? { ...prev, description } : prev));
            setEditDesc(false);
          }}
        />
      )}
      <Confirm
        open={removeDs !== null}
        title={t("knowledge.detail.sources.removeTitle")}
        body={t("knowledge.detail.sources.removeBody", { name: removeDs?.name ?? "" })}
        confirmLabel={t("knowledge.detail.sources.removeSource")}
        danger
        busy={removing}
        onConfirm={() => void removeSource()}
        onClose={() => setRemoveDs(null)}
      />
      {del.dialog}
    </>
  );
}
