import type { CSSProperties } from "react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Chip, Panel } from "../../components";
import type { ChipTone } from "../../components";
import type {
  InspectedSkill,
  SkillLabGateMetric,
  SkillLabJobBody,
  SkillLabJobInfo,
  SkillLabJudgeMode,
  SkillLabStatus,
  SkillLabTargetBackend,
  SkillLabTasksetInfo,
} from "../../lib/api";
import { api, ApiError } from "../../lib/api";
import type { RegistryRecord } from "../Registry";
import { BackendModelFields } from "./BackendModelFields";

const GATE_METRICS: SkillLabGateMetric[] = ["hard", "soft", "mixed"];

/** Vendored skilleval default (train.batch_size) — one optimizer step per batch. */
const TRAIN_BATCH_SIZE = 4;

/** The loader's ratio split for a single-mode set, mirrored for the cost line. */
const SINGLE_SPLIT = { train: 0.4, val: 0.3 };

const STATUS_TONE: Record<string, ChipTone> = {
  DRAFT: "muted",
  PENDING_APPROVAL: "warn",
  APPROVED: "good",
  REJECTED: "crit",
  DEPRECATED: "muted",
};

/** Task counts the run will actually touch — a single-mode set is auto-split. */
function splitCounts(info: SkillLabTasksetInfo | null): { train: number; val: number } {
  if (info === null) return { train: 0, val: 0 };
  if (info.mode === "single") {
    const total = info.counts.tasks ?? 0;
    return {
      train: Math.round(total * SINGLE_SPLIT.train),
      val: Math.round(total * SINGLE_SPLIT.val),
    };
  }
  return { train: info.counts.train ?? 0, val: info.counts.val ?? 0 };
}

/**
 * Submit form for an optimization run. Deliberately a sibling of EvalWizard
 * rather than a shared abstraction: the two share the source/task-set pickers
 * but diverge on split selection (training owns the whole set) and on their
 * parameter sets, and one merged component would be harder to read than both.
 */
export function TrainWizard({
  status,
  provisioned,
  presetRecordId,
  onCreated,
  onCancel,
}: {
  /** carries the platform's model defaults; null until the shell's fetch lands */
  status: SkillLabStatus | null;
  /** false disables submit — the exec worker or the interpreter is missing. */
  provisioned: boolean;
  presetRecordId: string | null;
  onCreated: (job: SkillLabJobInfo) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();

  const [sourceTab, setSourceTab] = useState<"registry" | "upload">("registry");
  const [records, setRecords] = useState<RegistryRecord[] | null>(null);
  const [recordQuery, setRecordQuery] = useState("");
  const [recordId, setRecordId] = useState(presetRecordId ?? "");

  const [staging, setStaging] = useState<{ id: string; skills: InspectedSkill[] } | null>(null);
  const [stagedIndex, setStagedIndex] = useState<number | null>(null);
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const [tasksets, setTasksets] = useState<SkillLabTasksetInfo[]>([]);
  const [tasksetId, setTasksetId] = useState("");

  // trainable_files candidates (upstream studio parity): .md files inside the
  // skill, minus SKILL.md (which is always trainable). Unchecked = SKILL.md-only.
  const [candidateFiles, setCandidateFiles] = useState<string[]>([]);
  const [trainableFiles, setTrainableFiles] = useState<string[]>([]);

  const [epochs, setEpochs] = useState(1);
  const [learningRate, setLearningRate] = useState(4);
  const [gateMetric, setGateMetric] = useState<SkillLabGateMetric>("hard");
  const [targetBackend, setTargetBackend] = useState<SkillLabTargetBackend>("claude_code_exec");
  const [judgeMode, setJudgeMode] = useState<SkillLabJudgeMode>("auto");
  const [targetModel, setTargetModel] = useState("");
  const [judgeModel, setJudgeModel] = useState("");
  const [workers, setWorkers] = useState(2);
  const [timeout, setTimeoutSeconds] = useState(900);
  const [limit, setLimit] = useState(0);
  const [advanced, setAdvanced] = useState(false);

  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Raw fetch, and every status is trainable: the page has no typed registry
  // client and a DRAFT skill is exactly what someone wants to improve.
  const loadRecords = useCallback(async () => {
    try {
      const res = await fetch("/api/registry/records?type=AGENT_SKILLS");
      if (!res.ok) {
        setRecords([]);
        return;
      }
      const body = (await res.json()) as { records: RegistryRecord[] };
      setRecords(body.records);
    } catch {
      setRecords([]);
    }
  }, []);

  useEffect(() => {
    void loadRecords();
    api
      .skillLabTasksets()
      .then((rows) => {
        setTasksets(rows);
        // Prefer a split set: it carries the held-out val split the gate wants.
        const preferred = rows.find((row) => row.mode === "split") ?? rows[0];
        setTasksetId((prev) => prev || (preferred?.id ?? ""));
      })
      .catch(() => setTasksets([]));
  }, [loadRecords]);

  // Model defaults come from the platform config so an operator override is what
  // the wizard shows — and a typed value is never overwritten.
  useEffect(() => {
    if (status === null) return;
    setTargetModel((prev) => prev || status.default_target_model);
    setJudgeModel((prev) => prev || status.default_judge_model);
  }, [status]);

  // Candidate files follow the selected source. Registry: the record detail's
  // skillDefinition already carries the bundle's file list (no S3 round-trip);
  // upload: the staged inspection result carries it.
  useEffect(() => {
    setTrainableFiles([]);
    setCandidateFiles([]);
    const mdOnly = (files: string[]) =>
      files.filter((f) => f.toLowerCase().endsWith(".md") && f !== "SKILL.md");
    if (sourceTab === "upload") {
      const staged = staging?.skills.find((skill) => skill.index === stagedIndex);
      if (staged) setCandidateFiles(mdOnly(staged.files));
      return;
    }
    if (!recordId) return;
    let stale = false;
    fetch(`/api/registry/records/${encodeURIComponent(recordId)}`)
      .then(async (res) => {
        if (!res.ok || stale) return;
        const body = (await res.json()) as {
          descriptors?: { agentSkills?: { skillDefinition?: { inlineContent?: string } } };
        };
        const inline = body.descriptors?.agentSkills?.skillDefinition?.inlineContent;
        if (!inline) return;
        const definition = JSON.parse(inline) as { files?: string[] };
        if (!stale) setCandidateFiles(mdOnly(definition.files ?? []));
      })
      .catch(() => {
        /* no candidates — the section simply stays hidden */
      });
    return () => {
      stale = true;
    };
  }, [sourceTab, recordId, staging, stagedIndex]);

  const toggleTrainable = (file: string) =>
    setTrainableFiles((prev) =>
      prev.includes(file) ? prev.filter((f) => f !== file) : [...prev, file],
    );

  const selectedTaskset = tasksets.find((row) => row.id === tasksetId) ?? null;
  const counts = splitCounts(selectedTaskset);
  // One optimizer step per minibatch, so the val split is re-scored more often
  // than once an epoch; +2 covers the seed baseline and the final test pass.
  const steps = epochs * Math.max(1, Math.ceil(counts.train / TRAIN_BATCH_SIZE));
  const estimate = counts.train * epochs + counts.val * (steps + 2);

  const visibleRecords = (records ?? []).filter((record) => {
    const needle = recordQuery.trim().toLowerCase();
    if (!needle) return true;
    return (
      record.name.toLowerCase().includes(needle) ||
      record.description.toLowerCase().includes(needle)
    );
  });

  const onZip = async (file: File) => {
    setUploadBusy(true);
    setUploadError(null);
    setStaging(null);
    setStagedIndex(null);
    try {
      const result = await api.inspectSkillZip(file);
      setStaging({ id: result.staging_id, skills: result.skills });
      const firstValid = result.skills.find((skill) => skill.valid);
      setStagedIndex(firstValid ? firstValid.index : null);
    } catch (err) {
      setUploadError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setUploadBusy(false);
    }
  };

  const skillSource = (): SkillLabJobBody["skill_source"] | null => {
    if (sourceTab === "registry") return recordId ? { kind: "registry", record_id: recordId } : null;
    if (staging && stagedIndex !== null)
      return { kind: "upload", staging_id: staging.id, index: stagedIndex };
    return null;
  };

  const submit = async () => {
    setError(null);
    const source = skillSource();
    if (source === null) {
      setError(t("skillLab.train.wizard.err.noSkill"));
      return;
    }
    if (!tasksetId) {
      setError(t("skillLab.train.wizard.err.noTaskset"));
      return;
    }
    setBusy(true);
    try {
      const job = await api.skillLabJobCreate({
        type: "train",
        skill_source: source,
        taskset_id: tasksetId,
        params: {
          target_backend: targetBackend,
          target_model: targetModel.trim(),
          judge_model: judgeModel.trim(),
          judge_mode: judgeMode,
          epochs,
          learning_rate: learningRate,
          gate_metric: gateMetric,
          workers,
          timeout,
          limit,
          ...(trainableFiles.length > 0 ? { trainable_files: trainableFiles } : {}),
        },
      });
      onCreated(job);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const numberField = (
    key: "epochs" | "learningRate" | "workers" | "timeout" | "limit",
    value: number,
    set: (n: number) => void,
    min: number,
    max: number,
  ) => (
    <div className="field" style={{ flex: 1, minWidth: 150 }}>
      <label>{t(`skillLab.train.wizard.field.${key}`)}</label>
      <input
        className="input mono"
        type="number"
        min={min}
        max={max}
        value={value}
        data-testid={`train-param-${key}`}
        onChange={(e) => set(Number(e.target.value))}
      />
      <span className="mono dim" style={{ fontSize: 10.5 }}>
        {t(`skillLab.train.wizard.hint.${key}`, { min, max })}
      </span>
    </div>
  );

  return (
    <Panel
      brk
      title={t("skillLab.train.wizard.title")}
      sub={t("skillLab.train.wizard.sub")}
      style={{ "--i": 0 } as CSSProperties}
    >
      {!provisioned && (
        <div className="note" style={{ marginBottom: 12 }} data-testid="train-wizard-blocked">
          <span className="i">[!]</span>
          <span>{t("skillLab.eval.wizard.blocked")}</span>
        </div>
      )}

      <div className="field">
        <label>{t("skillLab.train.wizard.field.skill")}</label>
        <div className="selchips">
          {(["registry", "upload"] as const).map((option) => (
            <button
              key={option}
              type="button"
              className={`selchip${sourceTab === option ? " on" : ""}`}
              style={{ cursor: "pointer" }}
              data-testid={`train-source-${option}`}
              onClick={() => setSourceTab(option)}
            >
              {t(`skillLab.eval.wizard.source.${option}`)}
            </button>
          ))}
        </div>
      </div>

      {/* Keyed: without it React reuses the one `<input>` across the two
          branches and warns that a controlled field went uncontrolled. */}
      {sourceTab === "registry" ? (
        <div className="field" key="source-registry">
          <input
            className="input"
            value={recordQuery}
            placeholder={t("skillLab.eval.wizard.searchSkills")}
            data-testid="train-skill-search"
            style={{ marginBottom: 8 }}
            onChange={(e) => setRecordQuery(e.target.value)}
          />
          <div
            style={{ maxHeight: 240, overflowY: "auto", border: "1px solid var(--grid)" }}
            data-testid="train-skill-list"
          >
            <table>
              <tbody>
                {visibleRecords.map((record) => (
                  <tr
                    key={record.record_id}
                    data-testid={`train-skill-row-${record.record_id}`}
                    style={{
                      cursor: "pointer",
                      background:
                        recordId === record.record_id ? "rgba(255,176,0,.045)" : undefined,
                    }}
                    onClick={() => setRecordId(record.record_id)}
                  >
                    <td className="pri">
                      {recordId === record.record_id ? "◉" : "○"} {record.name}
                    </td>
                    <td className="dim" style={{ fontSize: 10.5 }}>
                      {record.description || "—"}
                    </td>
                    <td className="mono dim">{record.version ?? "—"}</td>
                    <td>
                      <Chip tone={STATUS_TONE[record.status] ?? "muted"}>{record.status}</Chip>
                    </td>
                  </tr>
                ))}
                {records === null && (
                  <tr>
                    <td colSpan={4} className="dim mono" style={{ textAlign: "center" }}>
                      {t("common.loading")}
                    </td>
                  </tr>
                )}
                {records !== null && visibleRecords.length === 0 && (
                  <tr>
                    <td colSpan={4} className="dim mono" style={{ textAlign: "center" }}>
                      {t("skillLab.eval.wizard.noSkills")}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <span className="mono dim" style={{ fontSize: 10.5 }}>
            {t("skillLab.eval.wizard.anyStatus")}
          </span>
        </div>
      ) : (
        <div className="field" key="source-upload">
          <input
            type="file"
            accept=".zip,application/zip"
            className="input"
            data-testid="train-skill-upload"
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) void onZip(file);
            }}
          />
          <span className="mono dim" style={{ fontSize: 10.5 }}>
            {t("skillLab.eval.wizard.uploadHint")}
          </span>
          {uploadBusy && (
            <span className="mono dim" style={{ fontSize: 10.5 }}>
              {t("skillLab.eval.wizard.inspecting")}
            </span>
          )}
          {uploadError !== null && (
            <div
              className="note"
              data-testid="train-upload-error"
              style={{ borderColor: "var(--crit)", marginTop: 8 }}
            >
              <span className="i" style={{ color: "var(--crit)" }}>
                [✕]
              </span>
              <span className="mono" style={{ fontSize: 10.5 }}>
                {uploadError}
              </span>
            </div>
          )}
          {staging !== null && (
            <div style={{ marginTop: 8 }} data-testid="train-staged-skills">
              {staging.skills.map((skill) => (
                <div
                  key={skill.index}
                  style={{
                    border: `1px solid ${skill.valid ? "var(--grid)" : "var(--crit)"}`,
                    padding: "8px 10px",
                    marginBottom: 6,
                    cursor: skill.valid ? "pointer" : "not-allowed",
                    background: stagedIndex === skill.index ? "rgba(255,176,0,.045)" : undefined,
                  }}
                  data-testid={`train-staged-skill-${skill.index}`}
                  onClick={() => skill.valid && setStagedIndex(skill.index)}
                >
                  <div className="mono" style={{ fontSize: 11 }}>
                    {stagedIndex === skill.index ? "◉" : "○"} {skill.name || "—"}{" "}
                    <span className="dim">{skill.version}</span>
                  </div>
                  <div className="dim" style={{ fontSize: 10.5 }}>
                    {skill.description || "—"}
                  </div>
                  {skill.errors.length > 0 && (
                    <div className="mono" style={{ color: "var(--crit)", fontSize: 10.5 }}>
                      {skill.errors.join(" · ")}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
          <div className="note" style={{ marginTop: 8 }} data-testid="train-upload-note">
            <span className="i">[i]</span>
            <span>{t("skillLab.train.wizard.uploadNote")}</span>
          </div>
        </div>
      )}

      {candidateFiles.length > 0 && (
        <div className="field" data-testid="train-trainable-files">
          <label>{t("skillLab.train.wizard.field.trainableFiles")}</label>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))",
              gap: 6,
            }}
          >
            {candidateFiles.map((file) => (
              <label
                key={file}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "6px 10px",
                  border: `1px solid ${trainableFiles.includes(file) ? "var(--good)" : "var(--grid)"}`,
                  cursor: "pointer",
                  minWidth: 0,
                }}
                data-testid={`train-trainable-${file}`}
              >
                <input
                  type="checkbox"
                  checked={trainableFiles.includes(file)}
                  onChange={() => toggleTrainable(file)}
                />
                <span className="mono" style={{ fontSize: 11, overflowWrap: "anywhere" }}>
                  {file}
                </span>
              </label>
            ))}
          </div>
          <span className="mono dim" style={{ fontSize: 10.5 }}>
            {t("skillLab.train.wizard.hint.trainableFiles", { n: trainableFiles.length })}
          </span>
        </div>
      )}

      <div className="field">
        <label>{t("skillLab.eval.wizard.field.taskset")}</label>
        <select
          className="input"
          value={tasksetId}
          data-testid="train-taskset-select"
          onChange={(e) => setTasksetId(e.target.value)}
        >
          <option value="" style={{ background: "#141816" }}>—</option>
          {tasksets.map((row) => (
            <option key={row.id} value={row.id} style={{ background: "#141816" }}>
              {row.name} ({row.mode})
            </option>
          ))}
        </select>
        {tasksets.length === 0 && (
          <span className="mono dim" style={{ fontSize: 10.5 }}>
            {t("skillLab.eval.wizard.noTasksets")}
          </span>
        )}
        {selectedTaskset !== null && (
          <span className="mono dim" style={{ fontSize: 10.5 }} data-testid="train-split-note">
            {selectedTaskset.mode === "single"
              ? t("skillLab.train.wizard.autoSplit", {
                  train: counts.train,
                  val: counts.val,
                })
              : t("skillLab.train.wizard.splitCounts", {
                  train: counts.train,
                  val: counts.val,
                  test: selectedTaskset.counts.test ?? 0,
                })}
          </span>
        )}
      </div>

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        {numberField("epochs", epochs, setEpochs, 1, 10)}
        {numberField("learningRate", learningRate, setLearningRate, 1, 16)}
        <div className="field" style={{ flex: 1, minWidth: 150 }}>
          <label>{t("skillLab.train.wizard.field.gateMetric")}</label>
          <select
            className="input"
            value={gateMetric}
            data-testid="train-param-gateMetric"
            onChange={(e) => setGateMetric(e.target.value as SkillLabGateMetric)}
          >
            {GATE_METRICS.map((option) => (
              <option key={option} value={option} style={{ background: "#141816" }}>
                {option}
              </option>
            ))}
          </select>
          <span className="mono dim" style={{ fontSize: 10.5 }}>
            {t("skillLab.train.wizard.hint.gateMetric")}
          </span>
        </div>
      </div>

      {selectedTaskset !== null && (
        <div className="note" style={{ marginTop: 4 }} data-testid="train-cost-line">
          <span className="i">[~]</span>
          <span>{t("skillLab.train.wizard.estimate", { n: estimate })}</span>
        </div>
      )}

      <div className="field" style={{ marginTop: 10 }}>
        <button
          type="button"
          className="selchip"
          style={{ cursor: "pointer" }}
          data-testid="train-advanced-toggle"
          onClick={() => setAdvanced((open) => !open)}
        >
          {advanced ? "▾" : "▸"} {t("skillLab.eval.wizard.advanced")}
        </button>
        {advanced && (
          <>
            <div style={{ marginTop: 8 }}>
              <BackendModelFields
                status={status}
                idPrefix="train"
                backend={targetBackend}
                setBackend={setTargetBackend}
                targetModel={targetModel}
                setTargetModel={setTargetModel}
                judgeModel={judgeModel}
                setJudgeModel={setJudgeModel}
                judgeMode={judgeMode}
                setJudgeMode={setJudgeMode}
              />
            </div>
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 8 }}>
              {numberField("workers", workers, setWorkers, 1, 8)}
              {numberField("timeout", timeout, setTimeoutSeconds, 60, 3600)}
              {numberField("limit", limit, setLimit, 0, 10000)}
            </div>
          </>
        )}
      </div>

      {error !== null && (
        <div
          className="note"
          data-testid="train-wizard-error"
          style={{ borderColor: "var(--crit)", margin: "10px 0" }}
        >
          <span className="i" style={{ color: "var(--crit)" }}>
            [✕]
          </span>
          <span>{error}</span>
        </div>
      )}

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 10 }}>
        <Btn data-testid="train-wizard-cancel" onClick={onCancel}>
          {t("common.cancel")}
        </Btn>
        <Btn
          primary
          disabled={busy || !provisioned}
          data-testid="train-wizard-submit"
          onClick={() => void submit()}
        >
          ▸ {busy ? t("skillLab.eval.wizard.submitting") : t("skillLab.train.wizard.submit")}
        </Btn>
      </div>
    </Panel>
  );
}
