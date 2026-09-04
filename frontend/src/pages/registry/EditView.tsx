import type { ChangeEvent, CSSProperties } from "react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Chip, Panel, useToast, ViewHead } from "../../components";
import type { ChipTone } from "../../components";
import { responseMessage } from "../../lib/api";
import type { RegistryRecord } from "../Registry";

interface EditViewProps {
  recordId: string;
  /** success tail: parent returns to the list, reloads, and selects the record */
  onDone: (record: RegistryRecord) => void;
  onBack: () => void;
}

interface SkillSourceMeta {
  kind: string;
  url?: string;
  ref?: string;
  subdir?: string;
  imported_at?: string;
}

interface InspectedSkill {
  index?: number;
  name: string;
  description: string;
  version: string;
  files: string[];
  skill_md_excerpt: string;
  valid: boolean;
  errors: string[];
}

interface InspectResponse {
  skills: InspectedSkill[];
  staging_id: string;
}

const SOURCE_CHIP_TONE: Record<string, ChipTone> = {
  inline: "muted",
  zip: "aqua",
  git: "amber",
  url: "blue",
};

const STATUS_CHIP: Record<string, { tone: ChipTone; icon: string; labelKey: string }> = {
  DRAFT: { tone: "muted", icon: "○", labelKey: "registry.states.draft" },
  PENDING_APPROVAL: { tone: "warn", icon: "◍", labelKey: "registry.states.submitted" },
  APPROVED: { tone: "good", icon: "●", labelKey: "registry.states.published" },
  REJECTED: { tone: "crit", icon: "✕", labelKey: "registry.states.rejected" },
  DEPRECATED: { tone: "muted", icon: "✕", labelKey: "registry.states.disabled" },
};

/** Parse the AGENT_SKILLS skillDefinition JSON; returns file list + source when present.
 *  Twin copy in ../Registry.tsx — keep both in sync if the shape changes. */
function parseSkillDefinition(
  record: RegistryRecord,
): { files: string[]; source: SkillSourceMeta | null } | null {
  if (record.type !== "AGENT_SKILLS") return null;
  const skills = record.descriptors?.agentSkills as
    | { skillDefinition?: { inlineContent?: string } }
    | undefined;
  const raw = skills?.skillDefinition?.inlineContent;
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as { files?: unknown; source?: unknown };
    const files = Array.isArray(parsed.files)
      ? parsed.files.filter((f): f is string => typeof f === "string")
      : [];
    const source =
      parsed.source && typeof parsed.source === "object"
        ? (parsed.source as SkillSourceMeta)
        : null;
    if (files.length === 0 && !source) return null;
    return { files, source };
  } catch {
    return null;
  }
}

/** MCP server url lives in descriptors.mcp.server.inlineContent → remotes[0].url. */
function parseMcpUrl(record: RegistryRecord): string {
  const mcp = record.descriptors?.mcp as { server?: { inlineContent?: string } } | undefined;
  const raw = mcp?.server?.inlineContent;
  if (!raw) return "";
  try {
    const parsed = JSON.parse(raw) as { remotes?: { url?: string }[]; url?: string };
    const remoteUrl = Array.isArray(parsed.remotes) ? parsed.remotes[0]?.url : undefined;
    return remoteUrl ?? parsed.url ?? "";
  } catch {
    return "";
  }
}

function parseSkillMd(record: RegistryRecord): string {
  const skills = record.descriptors?.agentSkills as
    | { skillMd?: { inlineContent?: string } }
    | undefined;
  return skills?.skillMd?.inlineContent ?? "";
}

/**
 * Standalone `?view=edit&record=<id>` sub-page (mirrors RegisterView's layout):
 * the edit form on the left, a read-only current-record summary on the right.
 * name is immutable (S3 prefix is keyed by it); content edits go through
 * `PUT /records/{id}` — SKILL.md inline (keeps supporting files) or a whole-zip
 * replace via the shared inspect→staging flow.
 */
export function EditView({ recordId, onDone, onBack }: EditViewProps) {
  const { t } = useTranslation();
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);

  const [record, setRecord] = useState<RegistryRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [desc, setDesc] = useState("");
  const [url, setUrl] = useState("");
  const [md, setMd] = useState("");
  const [mode, setMode] = useState<"md" | "zip">("md");
  const [origDesc, setOrigDesc] = useState("");
  const [origUrl, setOrigUrl] = useState("");
  const [origMd, setOrigMd] = useState("");

  // zip-replace staging (reuses the register inspect flow, single-skill zip)
  const [stagingId, setStagingId] = useState<string | null>(null);
  const [preview, setPreview] = useState<InspectedSkill | null>(null);
  const [inspecting, setInspecting] = useState(false);

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setLoadError(null);
    if (!recordId) {
      setLoading(false);
      setLoadError(t("registry.edit.notFound"));
      return;
    }
    void (async () => {
      try {
        const res = await fetch(`/api/registry/records/${recordId}`);
        if (!res.ok) {
          // Envelope-aware: an `aws.*` / `registry.*` code renders its console copy.
          const msg = await responseMessage(res);
          if (alive) setLoadError(t("registry.edit.loadFailed", { msg }));
          return;
        }
        const body = (await res.json()) as RegistryRecord;
        if (!alive) return;
        setRecord(body);
        setDesc(body.description ?? "");
        setOrigDesc(body.description ?? "");
        const u = parseMcpUrl(body);
        setUrl(u);
        setOrigUrl(u);
        const m = parseSkillMd(body);
        setMd(m);
        setOrigMd(m);
      } catch (err) {
        if (alive) setLoadError(t("registry.edit.loadFailed", { msg: String(err) }));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [recordId, t]);

  const handlePick = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (fileRef.current) fileRef.current.value = "";
    if (!file) return;
    setSaveError(null);
    setPreview(null);
    setStagingId(null);
    setInspecting(true);
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch("/api/registry/skills/inspect", { method: "POST", body: form });
      if (res.status === 410) {
        setSaveError(t("registry.edit.stagingExpired"));
        return;
      }
      const body = (await res.json().catch(() => ({}))) as InspectResponse & { message?: string };
      if (!res.ok) {
        setSaveError(body.message ?? `HTTP ${res.status}`);
        return;
      }
      const skill = body.skills?.[0];
      if (!skill) {
        setSaveError(t("registry.register.zipNoSkill"));
        return;
      }
      setStagingId(body.staging_id);
      setPreview(skill);
    } catch (err) {
      setSaveError(String(err));
    } finally {
      setInspecting(false);
    }
  };

  const clearZip = () => {
    setStagingId(null);
    setPreview(null);
    setSaveError(null);
    if (fileRef.current) fileRef.current.value = "";
  };

  const isMcp = record?.type === "MCP";
  const isSkill = record?.type === "AGENT_SKILLS";
  const descChanged = desc !== origDesc;
  const urlChanged = isMcp && url !== origUrl;
  const mdChanged = isSkill && mode === "md" && md !== origMd;
  const zipStaged = isSkill && mode === "zip" && stagingId !== null;
  const dirty = descChanged || urlChanged || mdChanged || zipStaged;
  const bundleInvalid = isSkill && mode === "zip" && !!preview && !preview.valid;
  const saveDisabledReason = !dirty
    ? t("registry.edit.disabledNoChanges")
    : bundleInvalid
      ? t("registry.edit.disabledInvalidBundle")
      : undefined;

  const save = async () => {
    if (!record || !dirty) return;
    setSaving(true);
    setSaveError(null);
    const body: Record<string, unknown> = {};
    if (descChanged) body.description = desc;
    if (urlChanged) body.url = url;
    if (mdChanged) body.skill_md = md;
    if (zipStaged && stagingId) {
      body.staging_id = stagingId;
      body.index = preview?.index ?? 0;
    }
    try {
      const res = await fetch(`/api/registry/records/${record.record_id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.status === 410) {
        clearZip();
        setSaveError(t("registry.edit.stagingExpired"));
        return;
      }
      const resBody = (await res.json().catch(() => ({}))) as RegistryRecord & { message?: string };
      if (!res.ok) {
        setSaveError(resBody.message ?? `HTTP ${res.status}`);
        return;
      }
      toast(t("registry.edit.saved", { name: record.name }));
      onDone(resBody);
    } catch (err) {
      setSaveError(String(err));
    } finally {
      setSaving(false);
    }
  };

  const backBtn = (
    <div style={{ marginBottom: 14 }}>
      <Btn onClick={onBack}>◂ {t("registry.title")}</Btn>
    </div>
  );

  if (loading) {
    return (
      <section>
        <ViewHead
          kicker={t("registry.kicker")}
          title={t("registry.edit.pageTitle", { name: "…" })}
        />
        {backBtn}
        <div className="dim mono" style={{ padding: 14 }}>
          {t("common.loading")}
        </div>
      </section>
    );
  }

  if (loadError || !record) {
    return (
      <section>
        <ViewHead
          kicker={t("registry.kicker")}
          title={t("registry.edit.pageTitle", { name: "—" })}
        />
        {backBtn}
        <Panel style={{ "--i": 0 } as CSSProperties}>
          <div
            className="note"
            style={{ color: "var(--crit)", borderColor: "var(--crit)" }}
            data-testid="edit-error"
          >
            <span className="i">!</span>
            <span>{loadError ?? t("registry.edit.notFound")}</span>
          </div>
        </Panel>
      </section>
    );
  }

  const statusChip = STATUS_CHIP[record.status] ?? STATUS_CHIP.DRAFT;
  const skillMeta = parseSkillDefinition(record);

  return (
    <section>
      <ViewHead
        kicker={t("registry.kicker")}
        title={t("registry.edit.pageTitle", { name: record.name })}
        meta={`${record.type} · ${t(statusChip.labelKey)}`}
      />
      {backBtn}
      <div className="eval-grid">
        <Panel
          brk
          title={t("registry.edit.title")}
          sub={t("registry.edit.pageMeta")}
          style={{ "--i": 0 } as CSSProperties}
        >
          <div data-testid="edit-form">
            <div className="field">
              <label>{t("registry.edit.name")}</label>
              <input className="input mono" value={record.name} readOnly disabled />
              <div className="note" style={{ marginTop: 8 }}>
                <span className="i">[i]</span>
                <span>{t("registry.edit.nameLocked")}</span>
              </div>
            </div>

            <div className="field">
              <label htmlFor="edit-desc">{t("registry.edit.description")}</label>
              <input
                id="edit-desc"
                className="input"
                value={desc}
                onChange={(e) => setDesc(e.target.value)}
                data-testid="edit-desc-input"
              />
            </div>

            {isMcp && (
              <div className="field">
                <label htmlFor="edit-url">{t("registry.edit.url")}</label>
                <input
                  id="edit-url"
                  className="input mono"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="https://mcp.example.com/sse"
                  data-testid="edit-url-input"
                />
              </div>
            )}

            {isSkill && (
              <div className="field">
                <label>{t("registry.edit.contentMode")}</label>
                <div className="selchips">
                  <button
                    type="button"
                    className={`selchip${mode === "md" ? " on" : ""}`}
                    style={{ cursor: "pointer" }}
                    onClick={() => {
                      setMode("md");
                      clearZip();
                    }}
                    data-testid="edit-mode-md"
                  >
                    {t("registry.edit.modeMd")}
                  </button>
                  <button
                    type="button"
                    className={`selchip${mode === "zip" ? " on" : ""}`}
                    style={{ cursor: "pointer" }}
                    onClick={() => setMode("zip")}
                    data-testid="edit-mode-zip"
                  >
                    {t("registry.edit.modeZip")}
                  </button>
                </div>
              </div>
            )}

            {isSkill && mode === "md" && (
              <div style={{ marginTop: 14 }}>
                <label htmlFor="edit-md">{t("registry.edit.mdContent")}</label>
                <textarea
                  id="edit-md"
                  className="input mono"
                  style={{ minHeight: 200, resize: "vertical", marginTop: 7 }}
                  value={md}
                  onChange={(e) => setMd(e.target.value)}
                  data-testid="edit-md-textarea"
                />
              </div>
            )}

            {isSkill && mode === "zip" && (
              <div style={{ marginTop: 14 }}>
                <div className="note" style={{ marginBottom: 10 }}>
                  <span className="i">[i]</span>
                  <span>{t("registry.edit.zipHint")}</span>
                </div>
                {!preview && (
                  <label className="btn" style={{ cursor: "pointer", justifyContent: "center" }}>
                    {inspecting ? t("registry.edit.zipInspecting") : t("registry.edit.zipPick")}
                    <input
                      ref={fileRef}
                      type="file"
                      accept=".zip"
                      style={{ display: "none" }}
                      disabled={inspecting}
                      data-testid="edit-zip-input"
                      onChange={(e) => void handlePick(e)}
                    />
                  </label>
                )}
                {preview && (
                  <div data-testid="edit-zip-preview">
                    <div className="kv">
                      <span className="k">{t("registry.edit.zipVersion")}</span>
                      <span className="v">{preview.version || "—"}</span>
                    </div>
                    <div className="field" style={{ marginTop: 10 }}>
                      <label>{t("registry.edit.zipFiles", { n: preview.files.length })}</label>
                      <div
                        className="code"
                        style={{ maxHeight: 160, overflowY: "auto" }}
                        data-testid="edit-zip-files"
                      >
                        {preview.files.join("\n")}
                      </div>
                    </div>
                    {(!preview.valid || preview.errors.length > 0) && (
                      <div
                        className="note"
                        style={{ color: "var(--crit)", borderColor: "var(--crit)" }}
                        data-testid="edit-zip-validation"
                      >
                        <span className="i">!</span>
                        <span>
                          {preview.errors.join("; ") || t("registry.register.invalidBundle")}
                        </span>
                      </div>
                    )}
                    <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 12 }}>
                      <Btn disabled={saving} onClick={clearZip} data-testid="edit-zip-reset-btn">
                        {t("registry.edit.zipReset")}
                      </Btn>
                    </div>
                  </div>
                )}
              </div>
            )}

            {saveError && (
              <div
                className="note"
                style={{ marginTop: 12, color: "var(--crit)", borderColor: "var(--crit)" }}
                data-testid="edit-error"
              >
                <span className="i">!</span>
                <span>{saveError}</span>
              </div>
            )}

            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 12 }}>
              <Btn
                primary
                disabled={saving || !dirty || bundleInvalid}
                disabledReason={saveDisabledReason}
                onClick={() => void save()}
                data-testid="edit-save-btn"
              >
                ▲ {saving ? t("registry.edit.saving") : t("registry.edit.save")}
              </Btn>
            </div>
          </div>
        </Panel>

        <Panel
          title={t("registry.edit.current.title")}
          sub={t("registry.edit.current.sub")}
          style={{ "--i": 1 } as CSSProperties}
        >
          <div className="kv">
            <span className="k">{t("registry.edit.current.status")}</span>
            <span className="v">
              <Chip tone={statusChip.tone} icon={statusChip.icon}>
                {t(statusChip.labelKey)}
              </Chip>
            </span>
          </div>
          <div className="kv">
            <span className="k">{t("registry.edit.current.version")}</span>
            <span className="v mono">{record.version ?? "—"}</span>
          </div>
          {skillMeta?.source && (
            <div className="kv">
              <span className="k">{t("registry.edit.current.source")}</span>
              <span className="v">
                <Chip tone={SOURCE_CHIP_TONE[skillMeta.source.kind] ?? "muted"}>
                  {skillMeta.source.kind}
                </Chip>
              </span>
            </div>
          )}
          <div className="kv">
            <span className="k">{t("registry.edit.current.updated")}</span>
            <span className="v">{record.updated_at?.slice(0, 19) ?? "—"}</span>
          </div>
          {skillMeta && skillMeta.files.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <label>{t("registry.edit.current.files", { n: skillMeta.files.length })}</label>
              <div
                className="code"
                style={{ maxHeight: 200, overflowY: "auto", marginTop: 7 }}
                data-testid="edit-current-files"
              >
                {skillMeta.files.join("\n")}
              </div>
            </div>
          )}
        </Panel>
      </div>
    </section>
  );
}
