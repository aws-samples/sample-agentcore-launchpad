import type { CSSProperties } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Chip, Panel } from "../../components";
import type { ChipTone } from "../../components";
import type {
  InspectedSkill,
  SkillLabJobBody,
  SkillLabJobInfo,
  SkillLabStatus,
  SkillLabTargetBackend,
  SkillLabTasksetInfo,
} from "../../lib/api";
import { api, ApiError } from "../../lib/api";
import type { RegistryRecord } from "../Registry";
import { BackendModelFields } from "./BackendModelFields";

const SPLIT_PREFERENCE = ["test", "val", "train"] as const;

const STATUS_TONE: Record<string, ChipTone> = {
  DRAFT: "muted",
  PENDING_APPROVAL: "warn",
  APPROVED: "good",
  REJECTED: "crit",
  DEPRECATED: "muted",
};

/** Splits a set actually carries, in run-preference order. */
const splitsOf = (info: SkillLabTasksetInfo): string[] =>
  info.mode === "single" ? [] : SPLIT_PREFERENCE.filter((split) => (info.counts[split] ?? 0) > 0);

export function EvalWizard({
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
  const [split, setSplit] = useState("");

  const [targetBackend, setTargetBackend] = useState<SkillLabTargetBackend>("claude_code_exec");
  const [targetModel, setTargetModel] = useState("");
  const [judgeModel, setJudgeModel] = useState("");
  const [workers, setWorkers] = useState(2);
  const [timeout, setTimeoutSeconds] = useState(600);
  const [limit, setLimit] = useState(0);
  const [advanced, setAdvanced] = useState(false);

  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Registry records are read raw: the page has no typed client and every
  // status is evaluable here (a DRAFT skill is exactly what a user wants to
  // try), so no `attachables` filtering.
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
        setTasksetId((prev) => prev || (rows[0]?.id ?? ""));
      })
      .catch(() => setTasksets([]));
  }, [loadRecords]);

  // Prefill the model fields from the platform config rather than a literal, so
  // an operator override of skill_lab_{target,judge}_model_id is what the wizard
  // shows — and submits. A field left empty means "use that default" (the
  // backend fills omitted/blank models), so a typed value is never overwritten.
  useEffect(() => {
    if (status === null) return;
    setTargetModel((prev) => prev || status.default_target_model);
    setJudgeModel((prev) => prev || status.default_judge_model);
  }, [status]);

  const selectedTaskset = tasksets.find((row) => row.id === tasksetId) ?? null;
  const availableSplits = useMemo(
    () => (selectedTaskset ? splitsOf(selectedTaskset) : []),
    [selectedTaskset],
  );

  // Follow the task set: a split carried over from the previous selection would
  // be refused by the backend (422 taskset_invalid).
  useEffect(() => {
    setSplit(availableSplits[0] ?? "");
  }, [availableSplits]);

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
      setError(t("skillLab.eval.wizard.err.noSkill"));
      return;
    }
    if (!tasksetId) {
      setError(t("skillLab.eval.wizard.err.noTaskset"));
      return;
    }
    setBusy(true);
    try {
      const job = await api.skillLabJobCreate({
        type: "eval",
        skill_source: source,
        taskset_id: tasksetId,
        ...(split ? { split } : {}),
        params: {
          target_backend: targetBackend,
          target_model: targetModel.trim(),
          judge_model: judgeModel.trim(),
          workers,
          timeout,
          limit,
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
    key: "workers" | "timeout" | "limit",
    value: number,
    set: (n: number) => void,
    min: number,
    max: number,
  ) => (
    <div className="field" style={{ flex: 1, minWidth: 130 }}>
      <label>{t(`skillLab.eval.wizard.field.${key}`)}</label>
      <input
        className="input mono"
        type="number"
        min={min}
        max={max}
        value={value}
        data-testid={`eval-param-${key}`}
        onChange={(e) => set(Number(e.target.value))}
      />
      <span className="mono dim" style={{ fontSize: 10.5 }}>
        {t(`skillLab.eval.wizard.hint.${key}`, { min, max })}
      </span>
    </div>
  );

  return (
    <Panel
      brk
      title={t("skillLab.eval.wizard.title")}
      sub={t("skillLab.eval.wizard.sub")}
      style={{ "--i": 0 } as CSSProperties}
    >
      {!provisioned && (
        <div className="note" style={{ marginBottom: 12 }} data-testid="eval-wizard-blocked">
          <span className="i">[!]</span>
          <span>{t("skillLab.eval.wizard.blocked")}</span>
        </div>
      )}

      <div className="field">
        <label>{t("skillLab.eval.wizard.field.skill")}</label>
        <div className="selchips">
          {(["registry", "upload"] as const).map((option) => (
            <button
              key={option}
              type="button"
              className={`selchip${sourceTab === option ? " on" : ""}`}
              style={{ cursor: "pointer" }}
              data-testid={`eval-source-${option}`}
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
            data-testid="eval-skill-search"
            style={{ marginBottom: 8 }}
            onChange={(e) => setRecordQuery(e.target.value)}
          />
          <div
            style={{ maxHeight: 240, overflowY: "auto", border: "1px solid var(--grid)" }}
            data-testid="eval-skill-list"
          >
            <table>
              <tbody>
                {visibleRecords.map((record) => (
                  <tr
                    key={record.record_id}
                    data-testid={`eval-skill-row-${record.record_id}`}
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
            data-testid="eval-skill-upload"
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
              data-testid="eval-upload-error"
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
            <div style={{ marginTop: 8 }} data-testid="eval-staged-skills">
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
                  data-testid={`eval-staged-skill-${skill.index}`}
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
          <div className="note" style={{ marginTop: 8 }}>
            <span className="i">[i]</span>
            <span>{t("skillLab.eval.wizard.uploadNote")}</span>
          </div>
        </div>
      )}

      <div className="field">
        <label>{t("skillLab.eval.wizard.field.taskset")}</label>
        <select
          className="input"
          value={tasksetId}
          data-testid="eval-taskset-select"
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
        {availableSplits.length > 0 && (
          <div className="selchips" style={{ marginTop: 8 }}>
            {availableSplits.map((option) => (
              <button
                key={option}
                type="button"
                className={`selchip${split === option ? " on" : ""}`}
                style={{ cursor: "pointer" }}
                data-testid={`eval-split-${option}`}
                onClick={() => setSplit(option)}
              >
                {option} ({selectedTaskset?.counts[option] ?? 0})
              </button>
            ))}
          </div>
        )}
      </div>

      <BackendModelFields
        status={status}
        idPrefix="eval"
        backend={targetBackend}
        setBackend={setTargetBackend}
        targetModel={targetModel}
        setTargetModel={setTargetModel}
        judgeModel={judgeModel}
        setJudgeModel={setJudgeModel}
      />

      <div className="field">
        <button
          type="button"
          className="selchip"
          style={{ cursor: "pointer" }}
          data-testid="eval-advanced-toggle"
          onClick={() => setAdvanced((open) => !open)}
        >
          {advanced ? "▾" : "▸"} {t("skillLab.eval.wizard.advanced")}
        </button>
        {advanced && (
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 8 }}>
            {numberField("workers", workers, setWorkers, 1, 8)}
            {numberField("timeout", timeout, setTimeoutSeconds, 60, 3600)}
            {numberField("limit", limit, setLimit, 0, 10000)}
          </div>
        )}
      </div>

      {error !== null && (
        <div
          className="note"
          data-testid="eval-wizard-error"
          style={{ borderColor: "var(--crit)", margin: "10px 0" }}
        >
          <span className="i" style={{ color: "var(--crit)" }}>
            [✕]
          </span>
          <span>{error}</span>
        </div>
      )}

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 10 }}>
        <Btn data-testid="eval-wizard-cancel" onClick={onCancel}>
          {t("common.cancel")}
        </Btn>
        <Btn
          primary
          disabled={busy || !provisioned}
          data-testid="eval-wizard-submit"
          onClick={() => void submit()}
        >
          ▸ {busy ? t("skillLab.eval.wizard.submitting") : t("skillLab.eval.wizard.submit")}
        </Btn>
      </div>
    </Panel>
  );
}
