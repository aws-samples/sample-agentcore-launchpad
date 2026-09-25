import { Upload } from "lucide-react";
import { type ChangeEvent, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, errorMessage, type RegistryInspectedSkill, type RegistryUpdateBody } from "../../../lib/api";
import { parseMcpUrl, parseSkillDefinition, parseSkillMd } from "../../../lib/registry";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Descriptions, Field, FlowHeader, OptionCard, Spin } from "../../ui";
import { isStagingExpired, type RegistryRecord } from "./common";
import { SourceTag, StatusTag, TypeTag } from "./tags";

interface Draft {
  desc: string;
  url: string;
  md: string;
}

function draftFrom(record: RegistryRecord): Draft {
  return { desc: record.description ?? "", url: parseMcpUrl(record), md: parseSkillMd(record) };
}

function CurrentRecord({ record }: { record: RegistryRecord }) {
  const { t } = useTranslation();
  const meta = parseSkillDefinition(record);
  return (
    <Card title={t("v2.registry.current")} sub={t("v2.registry.currentSub")}>
      <Descriptions
        one
        items={[
          { label: t("v2.registry.col.type"), value: <TypeTag type={record.type} /> },
          { label: t("v2.registry.col.status"), value: <StatusTag status={record.status} /> },
          { label: t("v2.registry.col.version"), value: <span className="mono">{record.version ?? "—"}</span> },
          ...(meta?.source ? [{ label: t("v2.registry.source.label"), value: <SourceTag kind={meta.source.kind} /> }] : []),
          { label: t("v2.registry.col.updated"), value: fmtTime(record.updated_at) },
        ]}
      />
      {meta && meta.files.length > 0 && (
        <>
          <div className="v2-sub-title">{t("v2.registry.files", { n: meta.files.length })}</div>
          <pre className="v2-pre" style={{ maxHeight: 220 }} data-testid="v2-registry-edit-current-files">{meta.files.join("\n")}</pre>
        </>
      )}
    </Card>
  );
}

/**
 * `?view=edit&id=` — description + content edits through `PUT /records/{id}`.
 * The name is immutable (the S3 prefix is keyed by it); a skill edits SKILL.md
 * inline (supporting files kept) or replaces the whole bundle with a new zip.
 */
export function RecordEdit({ id }: { id: string }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [, setParams] = useSearchParams();
  const detail = useLoad(() => api.registryRecord(id), `registry-edit:${id}`);
  const fileRef = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [mode, setMode] = useState<"md" | "zip">("md");
  const [staged, setStaged] = useState<{ stagingId: string; skill: RegistryInspectedSkill } | null>(null);
  const [inspecting, setInspecting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const record = detail.data;
  // seed the form once the record arrives
  if (record && draft === null) setDraft(draftFrom(record));
  const back = () => setParams({ view: "detail", id });

  if (!record || !draft) {
    return (
      <>
        <FlowHeader title={t("v2.registry.editTitle", { name: "…" })} onBack={back} />
        {detail.error ? <Alert tone="error">{t("registry.edit.loadFailed", { msg: detail.error })}</Alert> : <Spin />}
      </>
    );
  }

  // A system-managed Skill has no edit form: every PUT is refused server-side, so a
  // deep link lands on a read-only summary instead of a form that fails on save.
  if (record.system) {
    return (
      <>
        <FlowHeader title={t("v2.registry.editTitle", { name: record.name })} onBack={back} />
        <Alert tone="warn">{t("registry.edit.systemReadOnly", { label: record.system.label })}</Alert>
        <CurrentRecord record={record} />
      </>
    );
  }

  const orig = draftFrom(record);
  const isMcp = record.type === "MCP";
  const isSkill = record.type === "AGENT_SKILLS";
  const descChanged = draft.desc !== orig.desc;
  const urlChanged = isMcp && draft.url !== orig.url;
  const mdChanged = isSkill && mode === "md" && draft.md !== orig.md;
  const zipStaged = isSkill && mode === "zip" && staged !== null;
  const dirty = descChanged || urlChanged || mdChanged || zipStaged;
  const bundleInvalid = isSkill && mode === "zip" && staged !== null && !staged.skill.valid;
  const disabledReason = !dirty
    ? t("registry.edit.disabledNoChanges")
    : bundleInvalid
      ? t("registry.edit.disabledInvalidBundle")
      : undefined;
  const set = (patch: Partial<Draft>) => setDraft({ ...draft, ...patch });

  const clearZip = () => {
    setStaged(null);
    if (fileRef.current) fileRef.current.value = "";
  };

  const pick = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (fileRef.current) fileRef.current.value = "";
    if (!file) return;
    setError(null);
    setStaged(null);
    setInspecting(true);
    try {
      const body = await api.registryInspectZip(file);
      const skill = body.skills?.[0];
      if (!skill) setError(t("registry.register.zipNoSkill"));
      else setStaged({ stagingId: body.staging_id, skill });
    } catch (err) {
      setError(isStagingExpired(err) ? t("registry.edit.stagingExpired") : errorMessage(err));
    } finally {
      setInspecting(false);
    }
  };

  const save = async () => {
    if (!dirty || bundleInvalid) return;
    setSaving(true);
    setError(null);
    const body: RegistryUpdateBody = {};
    if (descChanged) body.description = draft.desc;
    if (urlChanged) body.url = draft.url;
    if (mdChanged) body.skill_md = draft.md;
    if (zipStaged && staged) {
      body.staging_id = staged.stagingId;
      body.index = staged.skill.index ?? 0;
    }
    try {
      await api.registryUpdate(record.record_id, body);
      toast("success", t("registry.edit.saved", { name: record.name }));
      setParams({ view: "detail", id: record.record_id });
    } catch (err) {
      if (isStagingExpired(err)) {
        clearZip();
        setError(t("registry.edit.stagingExpired"));
      } else {
        setError(errorMessage(err));
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            {t("v2.registry.editTitle", { name: record.name })}
            <StatusTag status={record.status} />
          </span>
        }
        onBack={back}
        end={
          <Button
            kind="primary"
            disabled={saving || !dirty || bundleInvalid}
            title={disabledReason}
            onClick={() => void save()}
            testId="v2-registry-edit-save"
          >
            {saving ? t("registry.edit.saving") : t("v2.common.save")}
          </Button>
        }
      />
      {error && <Alert tone="error">{error}</Alert>}
      <div className="v2-registry-layout">
        <div>
          <Card title={t("v2.registry.basic")}>
            <div className="v2-form cols-2">
              <Field label={t("v2.registry.field.name")} hint={t("registry.edit.nameLocked")}>
                <input className="v2-input mono" value={record.name} disabled />
              </Field>
              <Field label={t("v2.registry.field.description")} full>
                <input
                  className="v2-input"
                  value={draft.desc}
                  onChange={(e) => set({ desc: e.target.value })}
                  data-testid="v2-registry-edit-desc"
                />
              </Field>
              {isMcp && (
                <Field label={t("v2.registry.field.mcpUrl")} full hint={t("v2.registry.field.mcpUrlHint")}>
                  <input
                    className="v2-input mono"
                    value={draft.url}
                    onChange={(e) => set({ url: e.target.value })}
                    placeholder="https://mcp.example.com/sse"
                    data-testid="v2-registry-edit-url"
                  />
                </Field>
              )}
            </div>
          </Card>
          {isSkill && (
            <Card title={t("v2.registry.content")}>
              <div className="v2-options" style={{ marginBottom: 16 }}>
                <OptionCard
                  title={t("v2.registry.editMode.md")}
                  desc={t("v2.registry.editModeDesc.md")}
                  on={mode === "md"}
                  onClick={() => {
                    setMode("md");
                    clearZip();
                  }}
                  testId="v2-registry-edit-mode-md"
                />
                <OptionCard
                  title={t("v2.registry.editMode.zip")}
                  desc={t("v2.registry.editModeDesc.zip")}
                  on={mode === "zip"}
                  onClick={() => setMode("zip")}
                  testId="v2-registry-edit-mode-zip"
                />
              </div>
              {mode === "md" ? (
                <Field label={t("v2.registry.field.skillMd")} full>
                  <textarea
                    className="v2-textarea mono"
                    rows={14}
                    value={draft.md}
                    onChange={(e) => set({ md: e.target.value })}
                    data-testid="v2-registry-edit-md"
                  />
                </Field>
              ) : (
                <>
                  <Alert tone="warn">{t("registry.edit.zipHint")}</Alert>
                  {staged ? (
                    <div data-testid="v2-registry-edit-zip-preview">
                      {(!staged.skill.valid || staged.skill.errors.length > 0) && (
                        <Alert tone="error">{staged.skill.errors.join("; ") || t("registry.register.invalidBundle")}</Alert>
                      )}
                      <Descriptions
                        items={[
                          { label: t("v2.registry.field.version"), value: <span className="mono">{staged.skill.version || "—"}</span> },
                          { label: t("v2.registry.fileCountLabel"), value: staged.skill.files.length },
                        ]}
                      />
                      <pre className="v2-pre" style={{ maxHeight: 200, marginTop: 12 }}>{staged.skill.files.join("\n")}</pre>
                      <div style={{ marginTop: 12 }}>
                        <Button size="sm" disabled={saving} onClick={clearZip}>
                          {t("v2.registry.chooseAnother")}
                        </Button>
                      </div>
                    </div>
                  ) : (
                    <label className="v2-registry-drop" data-testid="v2-registry-edit-zip-pick">
                      <Upload size={22} aria-hidden="true" />
                      <span>{inspecting ? t("registry.edit.zipInspecting") : t("v2.registry.zipReplacePick")}</span>
                      <span className="v2-muted">{t("v2.registry.zipLimit")}</span>
                      <input
                        ref={fileRef}
                        type="file"
                        accept=".zip"
                        disabled={inspecting}
                        onChange={(e) => void pick(e)}
                        data-testid="v2-registry-edit-zip-input"
                      />
                    </label>
                  )}
                </>
              )}
            </Card>
          )}
        </div>
        <CurrentRecord record={record} />
      </div>
    </>
  );
}
