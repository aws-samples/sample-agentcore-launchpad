import type { CSSProperties } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import type { TFunction } from "i18next";
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
import type { TaskgenReviewDraft } from "./taskgenReview";
import { reviewBlocker, reviewSelection, toReviewDrafts } from "./taskgenReview";
import { TaskgenReviewEditor } from "./TaskgenReviewEditor";

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

/**
 * Localize a refused save. The server's stable code selects the sentence and
 * its `detail` supplies the ids/row numbers, so the Chinese UI never falls back
 * to the English server message while keeping every identifier the message named.
 * Validator and request-validation refusals list their per-row / per-field
 * diagnostics (split, row, id, field, limit) as plain text lines. The draft and
 * selection are untouched by any of this — the caller only stores the error.
 */
function describeSaveError(err: unknown, t: TFunction): string {
  if (!(err instanceof ApiError)) return String(err);
  // Helpers are nested on purpose: the function is self-contained so an external
  // probe can evaluate it alone (see self-evolution host probes).
  // Bounds for the diagnostics rendered from a refused save. Server detail is
  // untrusted text: it is coerced to plain strings, control characters stripped,
  // capped, and rendered as React text (never HTML). Anything left out is
  // disclosed as a count, never dropped silently.
  const MAX_ISSUE_LINES = 6;
  const MAX_ISSUE_CHARS = 240;

  function plainText(value: unknown, t: TFunction): string {
    const raw =
      typeof value === "string"
        ? value
        : typeof value === "number" || typeof value === "boolean"
          ? String(value)
          : "";
    // eslint-disable-next-line no-control-regex -- strip C0/C1 controls incl. newlines
    const clean = raw.replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ").trim();
    return clean.length > MAX_ISSUE_CHARS
      ? `${clean.slice(0, MAX_ISSUE_CHARS)}… ${t("skillLab.taskgen.err.truncated")}`
      : clean;
  }

  /** `{split, message}` rows from the task validator (skill_lab.taskset_invalid). */
  function validatorIssueLine(item: unknown, t: TFunction): string | null {
    if (item === null || typeof item !== "object" || Array.isArray(item)) return null;
    const { split, message } = item as { split?: unknown; message?: unknown };
    const text = plainText(message, t);
    if (!text) return null;
    const where = plainText(split, t);
    return where ? `${where}: ${text}` : text;
  }

  /** Pydantic rows `{loc, msg, ctx}` from FastAPI (validation.invalid_request):
   *  `["body","tasks",0,"id"]` → "tasks #1 · id", plus any numeric limits in ctx. */
  function requestIssueLine(item: unknown, t: TFunction): string | null {
    if (item === null || typeof item !== "object" || Array.isArray(item)) return null;
    const { loc, msg, ctx } = item as { loc?: unknown; msg?: unknown; ctx?: unknown };
    const text = plainText(msg, t);
    if (!text) return null;
    const parts = (Array.isArray(loc) ? loc : []).filter((part) => part !== "body");
    const where = parts
      .map((part, i) =>
        typeof part === "number" && parts[i - 1] === "tasks" ? `#${part + 1}` : plainText(part, t),
      )
      .filter(Boolean)
      .join(" · ");
    const limits =
      ctx !== null && typeof ctx === "object" && !Array.isArray(ctx)
        ? Object.entries(ctx as Record<string, unknown>)
            .filter(([, v]) => typeof v === "number" || typeof v === "string")
            .map(([k, v]) => `${plainText(k, t)} ${plainText(v, t)}`)
            .join(", ")
        : "";
    return `${where ? `${where}: ` : ""}${text}${limits ? ` (${limits})` : ""}`;
  }

  /** Bounded, disclosed list: at most MAX_ISSUE_LINES lines; unreadable entries and
   *  the overflow are each reported as a count. */
  function issueLines(
    detail: unknown,
    toLine: (item: unknown, t: TFunction) => string | null,
    t: TFunction,
  ): string[] {
    const items = Array.isArray(detail) ? detail : detail === null || detail === undefined ? [] : [detail];
    const lines: string[] = [];
    let unreadable = 0;
    for (const item of items) {
      const line = toLine(item, t);
      if (line === null) unreadable += 1;
      else lines.push(`• ${line}`);
    }
    const shown = lines.slice(0, MAX_ISSUE_LINES);
    if (lines.length > shown.length)
      shown.push(t("skillLab.taskgen.err.moreIssues", { n: lines.length - shown.length }));
    if (unreadable > 0) shown.push(t("skillLab.taskgen.err.unreadableIssues", { n: unreadable }));
    return shown;
  }

  const detail = (err.detail ?? {}) as {
    ids?: unknown;
    reason?: string;
    index?: unknown;
    count?: unknown;
  };
  const ids = Array.isArray(detail.ids) ? detail.ids.map(String).join(", ") : "";
  switch (err.code) {
    case "skill_lab.taskgen_empty_selection":
      return t("skillLab.taskgen.review.noneKept");
    case "skill_lab.taskgen_duplicate_id":
      return t("skillLab.taskgen.review.duplicateIds", { ids: ids || err.message });
    case "skill_lab.expansion_conflict":
      return t("skillLab.taskgen.err.expansionConflict", { ids: ids || err.message });
    case "skill_lab.already_imported":
      return t("skillLab.taskgen.err.alreadySaved");
    case "skill_lab.taskgen_bad_selection":
      if (detail.reason === "out_of_range" || detail.reason === "repeated")
        return t(`skillLab.taskgen.err.badSelection.${detail.reason}`, {
          row: Number(detail.index) + 1,
          total: Number(detail.count),
        });
      return err.message;
    case "skill_lab.taskset_invalid": {
      const lines = issueLines(err.detail, validatorIssueLine, t);
      return [t("skillLab.taskgen.err.validatorRefused"), ...lines].join("\n");
    }
    case "validation.invalid_request": {
      const lines = issueLines(err.detail, requestIssueLine, t);
      return [t("skillLab.taskgen.err.requestRefused"), ...lines].join("\n");
    }
    default:
      return err.message;
  }
}

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
  // The raw failure, localized at render time so a language switch re-labels it.
  const [actionError, setActionError] = useState<unknown>(null);
  // Review drafts: what the save request will be built from. Derived from
  // `results` exactly once per fetched result set — the job poll only touches
  // `detail`, and a language change re-renders without refetching — so typed
  // edits survive both. Switching to another job clears `results` (below) and
  // therefore starts a fresh draft; coming back re-derives from the generated
  // rows, i.e. drafts are per visit, never persisted.
  const [drafts, setDrafts] = useState<TaskgenReviewDraft[] | null>(null);
  useEffect(() => {
    setDrafts(results === null ? null : toReviewDrafts(results.tasks));
  }, [results]);
  // Save in flight. The ref is the synchronous guard (a second click in the same
  // tick must not start a second POST); the state drives the disabled controls.
  // `viewGenRef` is a view generation: it advances every time the selected-job
  // effect runs AND when it cleans up (job switch, leaving the surface, unmount).
  // A save compares the generation it started under with the current one, so an
  // outcome that resolves after the operator moved on — to another job, back to
  // the list, or even back to the SAME job — is dropped instead of navigating.
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const viewGenRef = useRef(0);

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
  // `resultsRequested` is per effect run, i.e. per selected job: reading the
  // `results` state here instead would see the PREVIOUS job's value when the
  // operator switches straight from one finished job to another, and the second
  // job's results would never be fetched.
  useEffect(() => {
    setDetail(null);
    setResults(null);
    setActionError(null);
    viewGenRef.current += 1;
    savingRef.current = false;
    setSaving(false);
    if (!jobId) return;
    let stale = false;
    let resultsRequested = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = async () => {
      try {
        const job = await api.skillLabJobGet(jobId);
        if (stale) return;
        setDetail(job);
        if (job.status === "succeeded" && !resultsRequested) {
          resultsRequested = true;
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
      viewGenRef.current += 1; // invalidates any save still in flight for this view
      if (timer) clearTimeout(timer);
    };
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

  // A failed save (validation, collision, …) shows its reason and leaves the
  // drafts exactly as typed — nothing here resets them.
  const runSave = async (
    request: (
      job: SkillLabJobInfo,
      current: TaskgenReviewDraft[],
    ) => Promise<{ job: SkillLabJobInfo; taskset: SkillLabTasksetInfo }>,
  ) => {
    if (!detail || drafts === null || savingRef.current) return;
    const startedGen = viewGenRef.current;
    const current = () => viewGenRef.current === startedGen;
    savingRef.current = true;
    setSaving(true);
    setActionError(null);
    try {
      const outcome = await request(detail, drafts);
      if (!current()) return; // operator moved on (or left); the server write stands
      setDetail(outcome.job);
      onImported(outcome.taskset.id);
    } catch (err) {
      if (!current()) return;
      setActionError(err);
    } finally {
      if (current()) {
        savingRef.current = false;
        setSaving(false);
      }
    }
  };

  const importAsNew = () =>
    runSave((job, current) =>
      api.skillLabTaskgenImport(job.id, importName.trim(), reviewSelection(current).tasks),
    );

  const applyExpansion = () =>
    runSave((job, current) => api.skillLabTaskgenApply(job.id, reviewSelection(current).tasks));

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
  // After a save the review is a read-only view of the generator's ORIGINAL
  // output — not of the selection that was written; that lives in the task set.
  const saved = Boolean(imported || expanded);
  const savedTasksetId = imported ?? detail?.taskset_id ?? "";
  const selection = drafts === null ? null : reviewSelection(drafts);
  const keptCount = selection?.kept.length ?? 0;
  const excludedCount = drafts === null ? 0 : drafts.length - keptCount;
  const blocker = drafts === null ? null : reviewBlocker(drafts, t);

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

      {results !== null && drafts !== null && (
        <div style={{ marginTop: 12 }} data-testid="taskgen-results">
          <div
            className="mono"
            style={{ fontSize: 11, marginBottom: 4 }}
            data-testid="taskgen-review-title"
          >
            {saved
              ? t("skillLab.taskgen.review.originalTitle", { n: results.count })
              : t("skillLab.taskgen.review.title", { n: results.count })}
          </div>
          <div className="dim" style={{ fontSize: 10.5, marginBottom: 6 }}>
            {saved
              ? t("skillLab.taskgen.review.originalNote")
              : t("skillLab.taskgen.review.editHint")}
          </div>
          <div style={{ maxHeight: 420, overflowY: "auto", padding: 2 }}>
            <TaskgenReviewEditor
              drafts={drafts}
              onChange={setDrafts}
              showAttachments={attachedNames.length > 0}
              readOnly={saved || saving}
            />
          </div>
          {!saved && (
            <div
              className="mono dim"
              style={{
                fontSize: 10.5,
                marginTop: 6,
                display: "flex",
                gap: 10,
                alignItems: "center",
              }}
              data-testid="taskgen-review-summary"
            >
              <span>
                {t("skillLab.taskgen.review.summary", { kept: keptCount, total: drafts.length })}
                {excludedCount > 0 &&
                  ` · ${t("skillLab.taskgen.review.excludedCount", { n: excludedCount })}`}
              </span>
              <Btn
                data-testid="taskgen-review-reset"
                disabled={!selection?.dirty || saving}
                onClick={() => setDrafts(toReviewDrafts(results.tasks))}
              >
                {t("skillLab.taskgen.review.reset")}
              </Btn>
            </div>
          )}

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

          {saved ? (
            <div className="note" style={{ marginTop: 10 }} data-testid="taskgen-imported">
              <span className="i">[✓]</span>
              <span>
                {expanded
                  ? t("skillLab.taskgen.review.applied", { name: detail.taskset_name })
                  : t("skillLab.taskgen.review.imported")}{" "}
                <a
                  style={{ cursor: "pointer", textDecoration: "underline" }}
                  data-testid="taskgen-saved-link"
                  onClick={() => onImported(savedTasksetId)}
                >
                  {expanded
                    ? t("skillLab.taskgen.review.openSaved", { name: detail.taskset_name })
                    : savedTasksetId}
                </a>
              </span>
            </div>
          ) : (
            <div style={{ display: "flex", gap: 8, alignItems: "flex-end", marginTop: 10 }}>
              {isExpansion ? (
                <Btn
                  primary
                  data-testid="taskgen-apply"
                  disabled={blocker !== null || saving}
                  disabledReason={blocker ?? undefined}
                  aria-busy={saving}
                  onClick={() => void applyExpansion()}
                >
                  ▸{" "}
                  {saving
                    ? t("skillLab.taskgen.review.saving")
                    : t("skillLab.taskgen.review.apply", { split: detail.split })}
                </Btn>
              ) : (
                <>
                  <div className="field" style={{ flex: 1, maxWidth: 360, marginBottom: 0 }}>
                    <label htmlFor="taskgen-import-name">
                      {t("skillLab.taskgen.review.name")}
                    </label>
                    <input
                      id="taskgen-import-name"
                      className="input"
                      value={importName}
                      disabled={saving}
                      data-testid="taskgen-import-name"
                      onChange={(e) => setImportName(e.target.value)}
                    />
                  </div>
                  <Btn
                    primary
                    disabled={!importName.trim() || blocker !== null || saving}
                    disabledReason={
                      blocker ??
                      (!importName.trim() ? t("skillLab.taskgen.review.nameRequired") : undefined)
                    }
                    aria-busy={saving}
                    data-testid="taskgen-import"
                    onClick={() => void importAsNew()}
                  >
                    ▸{" "}
                    {saving
                      ? t("skillLab.taskgen.review.saving")
                      : t("skillLab.taskgen.review.import")}
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
              <span className="mono" style={{ fontSize: 10.5, whiteSpace: "pre-wrap" }}>
                {describeSaveError(actionError, t)}
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
