import type { CSSProperties } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Chip, Panel } from "../../components";
import type { ChipTone } from "../../components";
import type {
  SkillLabAssetDescriptor,
  SkillLabJobInfo,
  SkillLabStatus,
  SkillLabTargetBackend,
  SkillLabTaskgenResults,
  SkillLabTasksetInfo,
} from "../../lib/api";
import { api, ApiError } from "../../lib/api";
import type { RegistryRecord } from "../Registry";
import { JobLogPane } from "./JobLogPane";

const JOB_POLL_MS = 2500;
const LIVE_STATUSES = ["queued", "running"];

const STATUS_CHIP: Record<string, { tone: ChipTone; icon: string }> = {
  queued: { tone: "muted", icon: "◍" },
  running: { tone: "warn", icon: "◐" },
  succeeded: { tone: "good", icon: "●" },
  failed: { tone: "crit", icon: "✕" },
  cancelled: { tone: "muted", icon: "✕" },
  interrupted: { tone: "warn", icon: "!" },
};

const BACKEND_LABELS: Record<SkillLabTargetBackend, string> = {
  claude_code_exec: "claude_code_exec — Claude Code CLI",
  codex_exec: "codex_exec — Codex CLI",
};

const excerpt = (text: unknown, max = 110) => {
  const value = typeof text === "string" ? text : "";
  return value.length > max ? `${value.slice(0, max)}…` : value;
};

/** Mirrors runner.TASKGEN_ATTACHMENT_DIR: where the agent (and later the
 *  evaluated agent) sees an attached document. */
const runtimeAttachmentDir = "data";

function modelDefault(status: SkillLabStatus | null, backend: SkillLabTargetBackend): string {
  if (status === null) return "";
  return backend === "codex_exec" ? status.default_codex_target_model : status.default_target_model;
}

/**
 * AI task-set generation (studio parity): pick registry skill(s), an exec
 * backend, and a count; the agent authors tasks on the AgentCore worker; the
 * result is reviewed here and only an explicit action saves it — as a new task
 * set, or appended to the expansion target chosen at submit time.
 */
export function TaskgenPanel({
  genParam,
  tasksets,
  onSelectJob,
  onImported,
}: {
  /** "new" opens the wizard; a job id shows that job; from `?gen=`. */
  genParam: string;
  tasksets: SkillLabTasksetInfo[];
  onSelectJob: (id: string | null) => void;
  /** after a successful import/apply — refresh the taskset list. */
  onImported: (tasksetId: string) => void;
}) {
  const { t } = useTranslation();

  const [status, setStatus] = useState<SkillLabStatus | null>(null);
  const [jobs, setJobs] = useState<SkillLabJobInfo[]>([]);
  const [records, setRecords] = useState<RegistryRecord[] | null>(null);
  const [recordQuery, setRecordQuery] = useState("");
  const [recordIds, setRecordIds] = useState<string[]>([]);

  const [backend, setBackend] = useState<SkillLabTargetBackend>("claude_code_exec");
  const [model, setModel] = useState("");
  const [count, setCount] = useState(5);
  const [guidance, setGuidance] = useState("");
  const [timeout, setTimeoutSeconds] = useState(900);
  const [expandId, setExpandId] = useState("");
  const [targetSplit, setTargetSplit] = useState("tasks");
  // Input documents the generation agent authors against. Staged through the
  // shared task-asset endpoint, so these rows are already verified descriptors.
  const [attachments, setAttachments] = useState<SkillLabAssetDescriptor[]>([]);
  const [attachBusy, setAttachBusy] = useState(false);
  const attachInput = useRef<HTMLInputElement>(null);

  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [detail, setDetail] = useState<SkillLabJobInfo | null>(null);
  const [results, setResults] = useState<SkillLabTaskgenResults | null>(null);
  const [importName, setImportName] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);

  // What the run was given vs what its tasks actually asked for. `params` is
  // recorded at submission so this works while a job is still running; the
  // gen_summary echo is the fallback for jobs submitted before that was stored.
  const attachedNames =
    detail?.params?.attachment_names ??
    (Array.isArray(results?.summary?.attachments)
      ? (results.summary.attachments as unknown[]).map(String)
      : []);
  const declaredNames = new Set(
    (results?.tasks ?? []).flatMap((task) =>
      Array.isArray(task.attachments) ? task.attachments.map(String) : [],
    ),
  );
  const unusedAttachments = attachedNames.filter((name) => !declaredNames.has(name));

  const creating = genParam === "new";
  const jobId = creating ? null : genParam;

  const loadJobs = useCallback(() => {
    api
      .skillLabJobs("taskgen")
      .then(setJobs)
      .catch(() => setJobs([]));
  }, []);

  useEffect(() => {
    loadJobs();
    api.skillLabStatus().then(setStatus).catch(() => setStatus(null));
    fetch("/api/registry/records?type=AGENT_SKILLS")
      .then((res) => (res.ok ? res.json() : { records: [] }))
      .then((body: { records: RegistryRecord[] }) => setRecords(body.records))
      .catch(() => setRecords([]));
  }, [loadJobs]);

  useEffect(() => {
    setModel((prev) => prev || modelDefault(status, backend));
  }, [status, backend]);

  // Selected job: fetch, then poll while live (results appear on success).
  useEffect(() => {
    setDetail(null);
    setResults(null);
    setActionError(null);
    if (!jobId) return;
    let stale = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = async () => {
      try {
        const job = await api.skillLabJobGet(jobId);
        if (stale) return;
        setDetail(job);
        if (job.status === "succeeded" && results === null) {
          api
            .skillLabTaskgenResults(jobId)
            .then((r) => !stale && setResults(r))
            .catch(() => undefined);
        }
        if (LIVE_STATUSES.includes(job.status)) {
          timer = setTimeout(() => void tick(), JOB_POLL_MS);
        } else {
          loadJobs();
        }
      } catch {
        if (!stale) setDetail(null);
      }
    };
    void tick();
    return () => {
      stale = true;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- results is written here
  }, [jobId, loadJobs]);

  const applyBackend = (next: SkillLabTargetBackend) => {
    if (next === backend) return;
    const previousDefault = modelDefault(status, backend);
    setBackend(next);
    if (!model.trim() || model === previousDefault) setModel(modelDefault(status, next));
  };

  const expandTarget = tasksets.find((row) => row.id === expandId) ?? null;
  const splitOptions =
    expandTarget === null
      ? []
      : expandTarget.mode === "single"
        ? ["tasks"]
        : ["train", "val", "test"];

  useEffect(() => {
    setTargetSplit(expandTarget?.mode === "split" ? "train" : "tasks");
  }, [expandTarget?.id, expandTarget?.mode]);

  const toggleRecord = (id: string) =>
    setRecordIds((prev) => (prev.includes(id) ? prev.filter((r) => r !== id) : [...prev, id]));

  const submit = async () => {
    setError(null);
    if (recordIds.length === 0) {
      setError(t("skillLab.taskgen.err.noSkill"));
      return;
    }
    setBusy(true);
    try {
      const job = await api.skillLabJobCreate({
        type: "taskgen",
        skill_source:
          recordIds.length === 1
            ? { kind: "registry", record_id: recordIds[0] }
            : { kind: "registry", record_ids: recordIds },
        ...(expandId ? { taskset_id: expandId, target_split: targetSplit } : {}),
        ...(attachments.length
          ? {
              attachments: attachments.map((asset) => ({
                staged_asset: String(asset.staged_asset),
              })),
            }
          : {}),
        params: {
          target_backend: backend,
          model: model.trim(),
          count,
          guidance: guidance.trim(),
          timeout,
        },
      });
      loadJobs();
      onSelectJob(job.id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const uploadAttachments = async (files: File[]) => {
    if (!files.length) return;
    setError(null);
    setAttachBusy(true);
    try {
      const response = await api.skillLabTaskAssetsUpload(files);
      setAttachments((prev) => [...prev, ...response.assets]);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setAttachBusy(false);
    }
  };

  const importAsNew = async () => {
    if (!detail) return;
    setActionError(null);
    try {
      const outcome = await api.skillLabTaskgenImport(detail.id, importName.trim());
      setDetail(outcome.job);
      onImported(outcome.taskset.id);
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : String(err));
    }
  };

  const applyExpansion = async () => {
    if (!detail) return;
    setActionError(null);
    try {
      const outcome = await api.skillLabTaskgenApply(detail.id);
      setDetail(outcome.job);
      onImported(outcome.taskset.id);
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : String(err));
    }
  };

  const visibleRecords = (records ?? []).filter((record) => {
    const needle = recordQuery.trim().toLowerCase();
    if (!needle) return true;
    return (
      record.name.toLowerCase().includes(needle) ||
      record.description.toLowerCase().includes(needle)
    );
  });

  const numberField = (
    key: "count" | "timeout",
    value: number,
    set: (n: number) => void,
    min: number,
    max: number,
  ) => (
    <div className="field" style={{ flex: 1, minWidth: 130 }}>
      <label>{t(`skillLab.taskgen.field.${key}`)}</label>
      <input
        className="input mono"
        type="number"
        min={min}
        max={max}
        value={value}
        data-testid={`taskgen-param-${key}`}
        onChange={(e) => set(Number(e.target.value))}
      />
      <span className="mono dim" style={{ fontSize: 10.5 }}>
        {t(`skillLab.taskgen.hint.${key}`, { min, max })}
      </span>
    </div>
  );

  const wizard = (
    <Panel
      brk
      title={t("skillLab.taskgen.wizard.title")}
      sub={t("skillLab.taskgen.wizard.sub")}
      style={{ "--i": 1 } as CSSProperties}
    >
      <div className="field">
        <label>{t("skillLab.taskgen.field.skills")}</label>
        <input
          className="input"
          value={recordQuery}
          placeholder={t("skillLab.eval.wizard.searchSkills")}
          data-testid="taskgen-skill-search"
          style={{ marginBottom: 8 }}
          onChange={(e) => setRecordQuery(e.target.value)}
        />
        <div
          style={{ maxHeight: 220, overflowY: "auto", border: "1px solid var(--grid)" }}
          data-testid="taskgen-skill-list"
        >
          <table>
            <tbody>
              {visibleRecords.map((record) => (
                <tr
                  key={record.record_id}
                  data-testid={`taskgen-skill-row-${record.record_id}`}
                  style={{
                    cursor: "pointer",
                    background: recordIds.includes(record.record_id)
                      ? "rgba(255,176,0,.045)"
                      : undefined,
                  }}
                  onClick={() => toggleRecord(record.record_id)}
                >
                  <td className="pri">
                    {recordIds.includes(record.record_id) ? "☑" : "☐"} {record.name}
                  </td>
                  <td className="dim" style={{ fontSize: 10.5 }}>
                    {excerpt(record.description, 70) || "—"}
                  </td>
                  <td className="mono dim">{record.version ?? "—"}</td>
                </tr>
              ))}
              {records === null && (
                <tr>
                  <td colSpan={3} className="dim mono" style={{ textAlign: "center" }}>
                    {t("common.loading")}
                  </td>
                </tr>
              )}
              {records !== null && visibleRecords.length === 0 && (
                <tr>
                  <td colSpan={3} className="dim mono" style={{ textAlign: "center" }}>
                    {t("skillLab.eval.wizard.noSkills")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <span className="mono dim" style={{ fontSize: 10.5 }}>
          {t("skillLab.taskgen.hint.skills")}
        </span>
      </div>

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <div className="field" style={{ flex: 1, minWidth: 240 }}>
          <label>{t("skillLab.backend.field")}</label>
          <select
            className="input"
            value={backend}
            data-testid="taskgen-param-targetBackend"
            onChange={(e) => applyBackend(e.target.value as SkillLabTargetBackend)}
          >
            {(status?.target_backends?.length
              ? status.target_backends
              : (Object.keys(BACKEND_LABELS) as SkillLabTargetBackend[])
            ).map((option) => (
              <option key={option} value={option} style={{ background: "#141816" }}>
                {BACKEND_LABELS[option] ?? option}
              </option>
            ))}
          </select>
        </div>
        <div className="field" style={{ flex: 1, minWidth: 240 }}>
          <label>{t("skillLab.eval.wizard.field.targetModel")}</label>
          <input
            className="input mono"
            value={model}
            data-testid="taskgen-param-model"
            onChange={(e) => setModel(e.target.value)}
          />
          <span className="mono dim" style={{ fontSize: 10.5 }}>
            {t(
              backend === "codex_exec"
                ? "skillLab.backend.targetHintCodex"
                : "skillLab.backend.targetHintClaude",
            )}
          </span>
        </div>
      </div>

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        {numberField("count", count, setCount, 1, 30)}
        {numberField("timeout", timeout, setTimeoutSeconds, 60, 3600)}
      </div>

      <div className="field">
        <label>{t("skillLab.taskgen.field.guidance")}</label>
        <textarea
          className="input mono"
          rows={2}
          value={guidance}
          placeholder={t("skillLab.taskgen.hint.guidance")}
          data-testid="taskgen-param-guidance"
          onChange={(e) => setGuidance(e.target.value)}
        />
      </div>

      <div className="field">
        <label>{t("skillLab.taskgen.field.attachments")}</label>
        <div>
          <Btn
            disabled={attachBusy}
            data-testid="taskgen-attach-btn"
            onClick={() => attachInput.current?.click()}
          >
            {attachBusy
              ? t("skillLab.taskgen.attach.uploading")
              : t("skillLab.taskgen.attach.pick")}
          </Btn>
          <span className="mono dim" style={{ fontSize: 10, marginLeft: 10 }}>
            {t("skillLab.taskgen.attach.hint")}
          </span>
        </div>
        <input
          ref={attachInput}
          type="file"
          multiple
          accept=".xlsx,.pdf,.png,.jpg,.jpeg,.webp,.md,.txt,.csv"
          style={{ display: "none" }}
          disabled={attachBusy}
          data-testid="taskgen-attach-input"
          onChange={(event) => {
            const picked = Array.from(event.target.files ?? []);
            event.target.value = "";
            void uploadAttachments(picked);
          }}
        />
        {attachments.map((asset) => (
          <div
            key={String(asset.staged_asset)}
            data-testid={`taskgen-attachment-${asset.name}`}
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              gap: 8,
              marginTop: 6,
            }}
          >
            <span className="mono dim" style={{ fontSize: 10.5 }}>
              {`${runtimeAttachmentDir}/${asset.name}`} · {asset.media_type} ·{" "}
              {asset.size.toLocaleString()} B
            </span>
            <Btn
              data-testid={`taskgen-attachment-remove-${asset.name}`}
              onClick={() =>
                setAttachments((prev) =>
                  prev.filter((row) => row.staged_asset !== asset.staged_asset),
                )
              }
            >
              {t("skillLab.taskgen.attach.remove")}
            </Btn>
          </div>
        ))}
      </div>

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <div className="field" style={{ flex: 1, minWidth: 240 }}>
          <label>{t("skillLab.taskgen.field.expand")}</label>
          <select
            className="input"
            value={expandId}
            data-testid="taskgen-expand-select"
            onChange={(e) => setExpandId(e.target.value)}
          >
            <option value="" style={{ background: "#141816" }}>{t("skillLab.taskgen.expand.none")}</option>
            {/* samples are read-only — expanding one would 409 at submit */}
            {tasksets.filter((row) => !row.sample).map((row) => (
              <option key={row.id} value={row.id} style={{ background: "#141816" }}>
                {row.name} ({row.mode})
              </option>
            ))}
          </select>
          <span className="mono dim" style={{ fontSize: 10.5 }}>
            {t("skillLab.taskgen.hint.expand")}
          </span>
        </div>
        {expandTarget !== null && (
          <div className="field" style={{ minWidth: 160 }}>
            <label>{t("skillLab.taskgen.field.targetSplit")}</label>
            <select
              className="input"
              value={targetSplit}
              data-testid="taskgen-target-split"
              onChange={(e) => setTargetSplit(e.target.value)}
            >
              {splitOptions.map((option) => (
                <option key={option} value={option} style={{ background: "#141816" }}>
                  {option}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      {error !== null && (
        <div
          className="note"
          data-testid="taskgen-wizard-error"
          style={{ borderColor: "var(--crit)", margin: "10px 0" }}
        >
          <span className="i" style={{ color: "var(--crit)" }}>
            [✕]
          </span>
          <span>{error}</span>
        </div>
      )}

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 10 }}>
        <Btn data-testid="taskgen-wizard-cancel" onClick={() => onSelectJob(null)}>
          {t("common.cancel")}
        </Btn>
        <Btn
          primary
          disabled={busy || !(status?.provisioned && status.venv_ready)}
          data-testid="taskgen-wizard-submit"
          onClick={() => void submit()}
        >
          ▸ {busy ? t("skillLab.taskgen.wizard.submitting") : t("skillLab.taskgen.wizard.submit")}
        </Btn>
      </div>
    </Panel>
  );

  const chip = detail ? (STATUS_CHIP[detail.status] ?? STATUS_CHIP.queued) : STATUS_CHIP.queued;
  const live = detail !== null && LIVE_STATUSES.includes(detail.status);
  const imported = detail?.params.imported_taskset_id;
  const expanded = detail?.params.expanded === true;
  const isExpansion = detail !== null && detail.taskset_id !== "";

  const jobPanel = detail !== null && (
    <Panel
      brk
      title={t("skillLab.taskgen.job.title", { id: detail.id })}
      sub={
        isExpansion
          ? t("skillLab.taskgen.job.expandSub", {
              name: detail.taskset_name,
              split: detail.split,
            })
          : t("skillLab.taskgen.job.newSub")
      }
      end={
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <Chip tone={chip.tone} icon={chip.icon}>
            {detail.progress || detail.status}
          </Chip>
          {live && (
            <Btn
              data-testid="taskgen-cancel-job"
              onClick={() => void api.skillLabJobCancel(detail.id).then(loadJobs)}
            >
              {t("skillLab.taskgen.job.cancel")}
            </Btn>
          )}
        </div>
      }
      style={{ "--i": 1 } as CSSProperties}
    >
      <div className="mono dim" style={{ fontSize: 10.5, marginBottom: 8 }}>
        {detail.skill_source?.names?.join(", ") ?? detail.skill_source?.name ?? "—"} ·{" "}
        {detail.params.target_backend ?? "claude_code_exec"} · {detail.params.model ?? "—"}
      </div>
      {detail.error !== null && (
        <div className="note" style={{ borderColor: "var(--crit)", marginBottom: 8 }}>
          <span className="i" style={{ color: "var(--crit)" }}>
            [✕]
          </span>
          <span className="mono" style={{ fontSize: 10.5 }}>
            {excerpt(detail.error, 400)}
          </span>
        </div>
      )}
      <JobLogPane jobId={detail.id} live={live} testId="taskgen-job-log" />

      {results !== null && (
        <div style={{ marginTop: 12 }} data-testid="taskgen-results">
          <div className="mono" style={{ fontSize: 11, marginBottom: 6 }}>
            {t("skillLab.taskgen.review.title", { n: results.count })}
          </div>
          <div style={{ maxHeight: 300, overflowY: "auto", border: "1px solid var(--grid)" }}>
            <table data-testid="taskgen-task-table">
              <thead>
                <tr>
                  <th>{t("skillLab.taskgen.col.id")}</th>
                  <th>{t("skillLab.taskgen.col.question")}</th>
                  <th>{t("skillLab.taskgen.col.rubric")}</th>
                  {attachedNames.length > 0 && (
                    <th>{t("skillLab.taskgen.col.attachments")}</th>
                  )}
                </tr>
              </thead>
              <tbody>
                {results.tasks.map((task, index) => (
                  <tr key={`${task.id}-${index}`}>
                    <td className="mono">{String(task.id ?? "")}</td>
                    <td style={{ fontSize: 11 }}>{excerpt(task.question)}</td>
                    <td className="dim" style={{ fontSize: 10.5 }}>
                      {excerpt(task.rubric)}
                    </td>
                    {attachedNames.length > 0 && (
                      <td
                        className="mono dim"
                        style={{ fontSize: 10 }}
                        data-testid={`taskgen-task-attachments-${String(task.id ?? "")}`}
                      >
                        {Array.isArray(task.attachments) && task.attachments.length
                          ? task.attachments.map(String).join(" · ")
                          : "—"}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {unusedAttachments.length > 0 && (
            <div
              className="note"
              style={{ marginTop: 10 }}
              data-testid="taskgen-unused-attachments"
            >
              <span className="i">[i]</span>
              <span>
                {t("skillLab.taskgen.review.unusedAttachments", {
                  names: unusedAttachments.join(", "),
                })}
              </span>
            </div>
          )}

          {imported || expanded ? (
            <div className="note" style={{ marginTop: 10 }} data-testid="taskgen-imported">
              <span className="i">[✓]</span>
              <span>
                {expanded
                  ? t("skillLab.taskgen.review.applied", { name: detail.taskset_name })
                  : t("skillLab.taskgen.review.imported")}{" "}
                {imported && (
                  <a
                    style={{ cursor: "pointer", textDecoration: "underline" }}
                    onClick={() => onImported(imported)}
                  >
                    {imported}
                  </a>
                )}
              </span>
            </div>
          ) : (
            <div style={{ display: "flex", gap: 8, alignItems: "flex-end", marginTop: 10 }}>
              {isExpansion ? (
                <Btn primary data-testid="taskgen-apply" onClick={() => void applyExpansion()}>
                  ▸ {t("skillLab.taskgen.review.apply", { split: detail.split })}
                </Btn>
              ) : (
                <>
                  <div className="field" style={{ flex: 1, maxWidth: 360, marginBottom: 0 }}>
                    <label>{t("skillLab.taskgen.review.name")}</label>
                    <input
                      className="input"
                      value={importName}
                      data-testid="taskgen-import-name"
                      onChange={(e) => setImportName(e.target.value)}
                    />
                  </div>
                  <Btn
                    primary
                    disabled={!importName.trim()}
                    data-testid="taskgen-import"
                    onClick={() => void importAsNew()}
                  >
                    ▸ {t("skillLab.taskgen.review.import")}
                  </Btn>
                </>
              )}
            </div>
          )}
          {actionError !== null && (
            <div
              className="note"
              data-testid="taskgen-action-error"
              style={{ borderColor: "var(--crit)", marginTop: 8 }}
            >
              <span className="i" style={{ color: "var(--crit)" }}>
                [✕]
              </span>
              <span className="mono" style={{ fontSize: 10.5 }}>
                {actionError}
              </span>
            </div>
          )}
        </div>
      )}
    </Panel>
  );

  return (
    <>
      <Panel
        brk
        pad={false}
        title={t("skillLab.taskgen.listTitle")}
        sub={t("skillLab.taskgen.listSub")}
        end={
          <Btn data-testid="taskgen-close" onClick={() => onSelectJob(null)}>
            {t("skillLab.taskgen.close")}
          </Btn>
        }
        style={{ "--i": 0, marginBottom: 14 } as CSSProperties}
      >
        <table data-testid="taskgen-job-table">
          <thead>
            <tr>
              <th>{t("skillLab.taskgen.col.job")}</th>
              <th>{t("skillLab.taskgen.col.skills")}</th>
              <th>{t("skillLab.taskgen.col.target")}</th>
              <th>{t("skillLab.taskgen.col.status")}</th>
              <th>{t("skillLab.taskgen.col.created")}</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => {
              const jobChip = STATUS_CHIP[job.status] ?? STATUS_CHIP.queued;
              return (
                <tr
                  key={job.id}
                  data-testid={`taskgen-job-row-${job.id}`}
                  onClick={() => onSelectJob(job.id)}
                  style={{
                    cursor: "pointer",
                    background: jobId === job.id ? "rgba(255,176,0,.045)" : undefined,
                  }}
                >
                  <td className="mono">{job.id}</td>
                  <td className="pri">
                    {job.skill_source?.names?.join(", ") ?? job.skill_source?.name ?? "—"}
                  </td>
                  <td className="mono dim">
                    {job.taskset_id
                      ? `${job.taskset_name} → ${job.split}`
                      : t("skillLab.taskgen.target.new")}
                  </td>
                  <td>
                    <Chip tone={jobChip.tone} icon={jobChip.icon}>
                      {job.status}
                    </Chip>
                  </td>
                  <td className="mono dim">
                    {job.created_at ? new Date(job.created_at).toLocaleString() : "—"}
                  </td>
                </tr>
              );
            })}
            {jobs.length === 0 && (
              <tr>
                <td colSpan={5} className="dim mono" style={{ textAlign: "center" }}>
                  {t("skillLab.taskgen.empty")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </Panel>

      {creating ? wizard : jobPanel}
    </>
  );
}
