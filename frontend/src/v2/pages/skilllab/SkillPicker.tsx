import { useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, errorMessage, type RegistryRecordOut } from "../../../lib/api";
import { Alert, Button, SearchInput, Segmented, Table, Tag, type TagTone } from "../../ui";
import { type SourceState, useSkillRecords } from "./state";

const RECORD_TONE: Record<string, TagTone> = {
  DRAFT: "gray",
  PENDING_APPROVAL: "orange",
  APPROVED: "green",
  REJECTED: "red",
  DEPRECATED: "gray",
};

function RecordTable({
  records,
  loading,
  selected,
  multi,
  onToggle,
  testId,
}: {
  records: RegistryRecordOut[];
  loading: boolean;
  selected: string[];
  multi: boolean;
  onToggle: (id: string) => void;
  testId: string;
}) {
  const { t } = useTranslation();
  const [q, setQ] = useState("");
  // a preset selection (Registry deep link) is listed first so it is visible on entry
  const [pinned] = useState(selected);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const hits = needle
      ? records.filter((r) => r.name.toLowerCase().includes(needle) || r.description.toLowerCase().includes(needle))
      : records;
    return [...hits.filter((r) => pinned.includes(r.record_id)), ...hits.filter((r) => !pinned.includes(r.record_id))];
  }, [records, q, pinned]);
  return (
    <>
      <div className="v2-toolbar">
        <SearchInput value={q} onChange={setQ} placeholder={t("skillLab.eval.wizard.searchSkills")} testId={`${testId}-search`} />
        <div className="end">
          <span className="v2-count">
            {multi ? t("v2.skillLab.picked", { count: selected.length }) : t("v2.common.total", { count: rows.length })}
          </span>
        </div>
      </div>
      <div className="v2-skilllab-pick" data-testid={testId}>
        <Table
          density="dense"
          columns={[
            {
              key: "name",
              title: t("skillLab.eval.col.skill"),
              render: (r: RegistryRecordOut) => (
                <label className="v2-check" data-testid={`${testId}-row-${r.record_id}`}>
                  <input
                    type={multi ? "checkbox" : "radio"}
                    name={`${testId}-pick`}
                    checked={selected.includes(r.record_id)}
                    onChange={() => onToggle(r.record_id)}
                  />
                  <span>
                    {r.name}
                    <span className="sub ellipsis" style={{ maxWidth: 520 }} title={r.description}>
                      {r.description || "—"}
                    </span>
                  </span>
                </label>
              ),
            },
            { key: "version", title: t("v2.skillLab.colVersion"), className: "nowrap mono", width: 130, render: (r) => r.version ?? "—" },
            {
              key: "status",
              title: t("skillLab.eval.col.status"),
              width: 150,
              render: (r) => <Tag tone={RECORD_TONE[r.status] ?? "gray"}>{r.status}</Tag>,
            },
          ]}
          rows={rows}
          rowKey={(r) => r.record_id}
          loading={loading}
          selectedKey={multi ? null : (selected[0] ?? null)}
          empty={t("skillLab.eval.wizard.noSkills")}
        />
      </div>
    </>
  );
}

/** Registry record (radio) or uploaded .zip for an eval / train job. */
export function SkillSourcePicker({
  value,
  onChange,
  testId,
  uploadNote,
}: {
  value: SourceState;
  onChange: (next: SourceState) => void;
  testId: string;
  uploadNote: string;
}) {
  const { t } = useTranslation();
  const records = useSkillRecords();
  const zipRef = useRef<HTMLInputElement>(null);
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const onZip = async (file: File) => {
    setUploadBusy(true);
    setUploadError(null);
    onChange({ ...value, staging: null, stagedIndex: null });
    try {
      const result = await api.inspectSkillZip(file);
      const firstValid = result.skills.find((skill) => skill.valid);
      onChange({
        ...value,
        staging: { id: result.staging_id, skills: result.skills },
        stagedIndex: firstValid ? firstValid.index : null,
      });
    } catch (err) {
      setUploadError(errorMessage(err));
    } finally {
      setUploadBusy(false);
    }
  };

  return (
    <>
      <div style={{ marginBottom: 12 }}>
        <Segmented
          value={value.tab}
          onChange={(tab) => onChange({ ...value, tab })}
          options={(["registry", "upload"] as const).map((o) => ({
            value: o,
            label: t(`skillLab.eval.wizard.source.${o}`),
          }))}
        />
      </div>
      {value.tab === "registry" ? (
        <>
          <RecordTable
            records={records.data ?? []}
            loading={records.loading}
            selected={value.recordId ? [value.recordId] : []}
            multi={false}
            onToggle={(id) => onChange({ ...value, recordId: id })}
            testId={`${testId}-skills`}
          />
          <p className="v2-muted v2-skilllab-hint">{t("skillLab.eval.wizard.anyStatus")}</p>
        </>
      ) : (
        <>
          <div className="v2-row">
            <Button disabled={uploadBusy} onClick={() => zipRef.current?.click()} testId={`${testId}-upload-btn`}>
              {uploadBusy ? t("skillLab.eval.wizard.inspecting") : t("skillLab.eval.wizard.uploadPick")}
            </Button>
            <span className="v2-muted">{t("skillLab.eval.wizard.uploadHint")}</span>
          </div>
          <input
            ref={zipRef}
            type="file"
            accept=".zip,application/zip"
            style={{ display: "none" }}
            data-testid={`${testId}-upload`}
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) void onZip(file);
            }}
          />
          {uploadError !== null && <Alert tone="error">{uploadError}</Alert>}
          {value.staging !== null && (
            <div className="v2-options" style={{ marginTop: 12 }} data-testid={`${testId}-staged`}>
              {value.staging.skills.map((skill) => (
                <button
                  key={skill.index}
                  type="button"
                  className={value.stagedIndex === skill.index ? "v2-option on" : "v2-option"}
                  disabled={!skill.valid}
                  onClick={() => onChange({ ...value, stagedIndex: skill.index })}
                  aria-pressed={value.stagedIndex === skill.index}
                >
                  <span className="t">
                    {skill.name || "—"} <span className="v2-muted mono">{skill.version}</span>
                  </span>
                  <span className="d">{skill.description || "—"}</span>
                  {skill.errors.length > 0 && <span className="d v2-skilllab-err">{skill.errors.join(" · ")}</span>}
                </button>
              ))}
            </div>
          )}
          <div style={{ marginTop: 12 }}>
            <Alert>{uploadNote}</Alert>
          </div>
        </>
      )}
    </>
  );
}

/** Multi-select registry picker (taskgen: one unified task set over several skills). */
export function SkillMultiPicker({
  selected,
  onChange,
  testId,
}: {
  selected: string[];
  onChange: (next: string[]) => void;
  testId: string;
}) {
  const records = useSkillRecords();
  return (
    <RecordTable
      records={records.data ?? []}
      loading={records.loading}
      selected={selected}
      multi
      onToggle={(id) => onChange(selected.includes(id) ? selected.filter((r) => r !== id) : [...selected, id])}
      testId={testId}
    />
  );
}
