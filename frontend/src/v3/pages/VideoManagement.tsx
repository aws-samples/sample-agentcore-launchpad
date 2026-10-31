import "./videomgmt.css";

import { ArrowLeft, Plus, RotateCw, ShieldAlert } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { api, ApiError, errorMessage } from "../../lib/api";
import type { ConsoleVersion, ManagedVideo, ManagedVideoCatalog, VideoContent, VideoLocale, VideoTaxonomy } from "../../lib/videos";
import { ago } from "../format";
import { useLoad, useToast } from "../hooks";
import { Reasoned } from "../reason";
import { Btn, Chip, Confirm, Empty, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Stat } from "../ui";
import { Select } from "../select";

const NEW = "new";
const emptyVideo = (): VideoContent => ({
  category_id: "",
  section_id: "",
  console_version: "v2",
  title: { en: "", "zh-CN": "" },
  description: { en: "", "zh-CN": "" },
  cdn_url: "",
  webm_url: null,
  poster_url: null,
  caption_url: null,
  duration_seconds: 0,
  chapters: [],
  sort_order: 1000,
});

type Action = "publish" | "unpublish" | "delete";

function useLocale(): VideoLocale {
  const { i18n } = useTranslation();
  return i18n.resolvedLanguage?.startsWith("zh") ? "zh-CN" : "en";
}

function directoryLabel(taxonomy: VideoTaxonomy, content: VideoContent, locale: VideoLocale): string {
  const category = taxonomy.categories.find((item) => item.id === content.category_id);
  const section = taxonomy.sections.find((item) => item.id === content.section_id);
  return [category?.title[locale], section?.title[locale]].filter(Boolean).join(" / ");
}

function rowSignal(row: ManagedVideo): Signal {
  if (row.status !== "published") return "off";
  return row.has_unpublished_changes ? "wait" : "ok";
}

function StatusChips({ row }: { row: ManagedVideo }) {
  const { t } = useTranslation();
  return (
    <span className="v3-vm-chips">
      <Chip s={row.status === "published" ? "ok" : "wait"}>{t(`videoManage.${row.status}`)}</Chip>
      {row.status === "published" && row.has_unpublished_changes && <Chip s="info">{t("videoManage.unpublishedChanges")}</Chip>}
    </span>
  );
}

/* ── list ────────────────────────────────────────────────────────────────── */

function List({
  catalog,
  loading,
  error,
  onRetry,
  onOpen,
}: {
  catalog: ManagedVideoCatalog | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onOpen: (id: string) => void;
}) {
  const { t } = useTranslation();
  const locale = useLocale();
  const rows = catalog?.videos ?? [];
  const published = rows.filter((row) => row.status === "published").length;
  const changed = rows.filter((row) => row.status === "published" && row.has_unpublished_changes).length;
  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.videomgmt.eyebrow")}
        title={t("videoManage.title")}
        sub={t("videoManage.description")}
        end={
          <>
            <Btn kind="ghost" onClick={onRetry}><RotateCw size={14} /></Btn>
            <Btn kind="primary" onClick={() => onOpen(NEW)}><Plus size={14} /> {t("videoManage.new")}</Btn>
          </>
        }
      />
      {error && catalog && <Notice s="act">{error} <Btn size="sm" onClick={onRetry}>{t("v3.videomgmt.retry")}</Btn></Notice>}
      <div className="v3-grid c3">
        <Panel><Stat label={t("v3.videomgmt.total")} value={catalog ? rows.length : "—"} /></Panel>
        <Panel signal={published ? "ok" : undefined}><Stat label={t("v3.videomgmt.published")} value={catalog ? published : "—"} foot={t("v3.videomgmt.publishedFoot")} /></Panel>
        <Panel signal={changed ? "wait" : undefined}>
          <Stat label={t("v3.videomgmt.drafts")} value={catalog ? rows.length - published : "—"}
            foot={t("v3.videomgmt.changedFoot", { count: changed })} />
        </Panel>
      </div>
      <Panel flush>
        {loading && !catalog ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : !catalog ? (
          <div style={{ padding: 20 }}><Notice s="act">{error} <Btn size="sm" onClick={onRetry}>{t("v3.videomgmt.retry")}</Btn></Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t("videoManage.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("videoManage.titleField")}</th>
                <th>{t("videoManage.directory")}</th>
                <th>{t("videoManage.consoleVersion")}</th>
                <th>{t("videoManage.status")}</th>
                <th className="num">{t("videoManage.updated")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const version = row.published_content?.console_version ?? row.content.console_version;
                return (
                  <tr key={row.id} className="click" onClick={() => onOpen(row.id)}>
                    <td style={{ width: 30 }}><Lamp s={rowSignal(row)} /></td>
                    <td><div className="v3-name"><div><b>{row.content.title[locale]}</b><small>{row.id}</small></div></div></td>
                    <td style={{ color: "var(--v3-text-2)" }}>{directoryLabel(catalog.taxonomy, row.content, locale) || "—"}</td>
                    <td><Chip s={version === "v3" ? "ok" : version === "v2" ? "info" : undefined}>{t(`videos.version.${version}`)}</Chip></td>
                    <td><StatusChips row={row} /></td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(row.updated_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}

/* ── editor ──────────────────────────────────────────────────────────────── */

function Editor({
  id,
  taxonomy,
  onBack,
  onCreated,
  onChanged,
}: {
  id: string;
  taxonomy: VideoTaxonomy;
  onBack: () => void;
  onCreated: (id: string) => void;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const locale = useLocale();
  const [row, setRow] = useState<ManagedVideo | null>(null);
  const [draft, setDraft] = useState<VideoContent>(emptyVideo);
  const [chaptersText, setChaptersText] = useState("[]");
  const [loading, setLoading] = useState(id !== NEW);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Action | null>(null);

  const load = useCallback(async () => {
    if (id === NEW) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const next = await api.getManagedVideo(id);
      setRow(next);
      setDraft(next.content);
      setChaptersText(JSON.stringify(next.content.chapters, null, 2));
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.code === "videos.not_found") setRow(null);
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [id]);
  useEffect(() => {
    void load();
  }, [load]);

  const update = <K extends keyof VideoContent>(field: K, value: VideoContent[K]) => setDraft((prev) => ({ ...prev, [field]: value }));
  const updateText = (field: "title" | "description", language: VideoLocale, value: string) =>
    setDraft((prev) => ({ ...prev, [field]: { ...prev[field], [language]: value } }));

  // the same gates as V2: a valid directory, the zh-CN title and description, an https mp4/webm
  const directoryOk = taxonomy.sections.some((s) => s.id === draft.section_id && s.categoryId === draft.category_id);
  const titleOk = Boolean(draft.title["zh-CN"].trim());
  const descriptionOk = Boolean(draft.description["zh-CN"].trim());
  const cdnOk = /^https:\/\/[^?#]+\.(mp4|webm)$/i.test(draft.cdn_url.trim());
  const dirty = !row || JSON.stringify(draft) !== JSON.stringify(row.content) || chaptersText !== JSON.stringify(row.content.chapters, null, 2);
  const saveReason = !draft.category_id
    ? "videoManage.chooseCategory"
    : !directoryOk
      ? "videoManage.chooseSection"
      : !titleOk
        ? "videoManage.titleRequired"
        : !descriptionOk
          ? "videoManage.descriptionRequired"
          : !cdnOk
            ? "videoManage.invalidCdn"
            : !dirty
              ? "videoManage.noChanges"
              : null;
  const publishReason = !row || dirty ? "videoManage.saveFirst" : row.status === "published" && !row.has_unpublished_changes ? "videoManage.alreadyPublished" : null;
  const canSave = !busy && !saveReason;
  const canPublish = !busy && !publishReason;
  const sections = taxonomy.sections.filter((s) => s.categoryId === draft.category_id);

  const save = async () => {
    if (!canSave) return;
    let chapters: VideoContent["chapters"];
    try {
      const parsed: unknown = JSON.parse(chaptersText);
      if (!Array.isArray(parsed)) throw new Error("chapters must be an array");
      chapters = parsed as VideoContent["chapters"];
    } catch {
      setError(t("videoManage.invalidChapters"));
      return;
    }
    const content: VideoContent = {
      ...draft,
      title: { ...draft.title, en: draft.title.en.trim() || draft.title["zh-CN"].trim() },
      description: { ...draft.description, en: draft.description.en.trim() || draft.description["zh-CN"].trim() },
      cdn_url: draft.cdn_url.trim(),
      webm_url: draft.webm_url?.trim() || null,
      poster_url: draft.poster_url?.trim() || null,
      caption_url: draft.caption_url?.trim() || null,
      chapters,
    };
    setBusy(true);
    try {
      const saved = row ? await api.saveVideo(row.id, content, row.revision) : await api.createVideo(content);
      setRow(saved);
      setDraft(saved.content);
      setChaptersText(JSON.stringify(saved.content.chapters, null, 2));
      setError(null);
      toast("ok", t("videoManage.saved"));
      onChanged();
      if (!row) onCreated(saved.id);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const act = async () => {
    if (!row || !confirm) return;
    setBusy(true);
    try {
      if (confirm === "delete") {
        await api.deleteVideo(row.id, row.revision);
        onChanged();
        onBack();
      } else {
        const next = confirm === "publish" ? await api.publishVideo(row.id, row.revision) : await api.unpublishVideo(row.id, row.revision);
        setRow(next);
        setDraft(next.content);
        setChaptersText(JSON.stringify(next.content.chapters, null, 2));
        onChanged();
      }
      setError(null);
      toast("ok", t(`videoManage.${confirm}Done`));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  const back = (
    <div>
      <button type="button" className="v3-btn ghost sm" onClick={onBack}><ArrowLeft size={14} /> {t("videoManage.title")}</button>
    </div>
  );
  if (loading && !row) return <div style={{ display: "grid", gap: 16 }}>{back}<Skeleton rows={6} /></div>;
  if (id !== NEW && !row) {
    return (
      <div style={{ display: "grid", gap: 16 }}>
        {back}
        <Notice s="act">{error ?? t("videoManage.notFound")} <Btn size="sm" onClick={() => void load()}>{t("v3.videomgmt.retry")}</Btn></Notice>
      </div>
    );
  }

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }} aria-busy={busy}>
      {back}
      <PageHead
        eyebrow={row ? `${t("v3.videomgmt.eyebrow")} · ${t("videoManage.revision")} ${row.revision}` : t("v3.videomgmt.eyebrow")}
        title={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 14 }}>
            {row && <Lamp s={rowSignal(row)} />}
            {row ? draft.title[locale] || t("videoManage.edit") : t("videoManage.new")}
          </span>
        }
        sub={row ? <StatusChips row={row} /> : undefined}
        end={
          <>
            {row && <Btn kind="ghost" onClick={() => void load()} disabled={busy}><RotateCw size={14} /></Btn>}
            {row && row.status === "published" && <Btn onClick={() => setConfirm("unpublish")} disabled={busy}>{t("videoManage.unpublish")}</Btn>}
            {row && <Btn kind="danger" onClick={() => setConfirm("delete")} disabled={busy}>{t("v3.videomgmt.delete")}</Btn>}
            <Btn kind="primary" onClick={() => setConfirm("publish")} busy={busy} disabled={!canPublish} disabledReason={publishReason ? t(publishReason) : null}>
              {t("videoManage.publish")}
            </Btn>
          </>
        }
      />
      {error && <Notice s="act">{error}{row && <> <Btn size="sm" onClick={() => void load()}>{t("videoManage.reload")}</Btn></>}</Notice>}
      <Notice>{t("videoManage.draftHint")}</Notice>

      <div className="v3-grid v3-split" style={{ alignItems: "start" }}>
        <Panel title={t("videoManage.edit")}>
          <fieldset className="v3-vm-form" disabled={busy}>
            <label className="v3-field">
              <span>{t("videoManage.consoleVersion")} *</span>
              <Select className="v3-select" value={draft.console_version} onChange={(e) => update("console_version", e.target.value as ConsoleVersion)}>
                <option value="v3">{t("videos.version.v3")}</option>
                <option value="v2">{t("videos.version.v2")}</option>
                <option value="classic">{t("videos.version.classic")}</option>
              </Select>
            </label>
            <div className="v3-grid c2" style={{ alignItems: "start" }}>
              <label className="v3-field">
                <span>{t("videoManage.category")} *</span>
                <Select className="v3-select" value={draft.category_id}
                  onChange={(e) => setDraft((prev) => ({ ...prev, category_id: e.target.value, section_id: "" }))}>
                  <option value="">{t("videoManage.chooseCategory")}</option>
                  {taxonomy.categories.map((c) => <option key={c.id} value={c.id}>{c.title[locale]}</option>)}
                </Select>
              </label>
              <label className="v3-field">
                <span>{t("videoManage.section")} *</span>
                <Reasoned disabledReason={t("story.reason.pickArea")}>
                  <Select className="v3-select" value={draft.section_id} disabled={!draft.category_id} onChange={(e) => update("section_id", e.target.value)}>
                    <option value="">{t("videoManage.chooseSection")}</option>
                    {sections.map((s) => <option key={s.id} value={s.id}>{s.title[locale]}</option>)}
                  </Select>
                </Reasoned>
              </label>
            </div>
            <div className="v3-grid c2" style={{ alignItems: "start" }}>
              <label className="v3-field">
                <span>{t("videoManage.titleZh")} *</span>
                <input className="v3-input" value={draft.title["zh-CN"]} maxLength={160} onChange={(e) => updateText("title", "zh-CN", e.target.value)} />
              </label>
              <label className="v3-field">
                <span>{t("videoManage.titleEn")}</span>
                <input className="v3-input" value={draft.title.en} maxLength={160} onChange={(e) => updateText("title", "en", e.target.value)} />
                <small className="v3-hint">{t("videoManage.englishFallback")}</small>
              </label>
            </div>
            <div className="v3-grid c2" style={{ alignItems: "start" }}>
              <label className="v3-field">
                <span>{t("videoManage.descriptionZh")} *</span>
                <textarea className="v3-input" rows={4} maxLength={2000} value={draft.description["zh-CN"]}
                  onChange={(e) => updateText("description", "zh-CN", e.target.value)} />
              </label>
              <label className="v3-field">
                <span>{t("videoManage.descriptionEn")}</span>
                <textarea className="v3-input" rows={4} maxLength={2000} value={draft.description.en}
                  onChange={(e) => updateText("description", "en", e.target.value)} />
                <small className="v3-hint">{t("videoManage.englishFallback")}</small>
              </label>
            </div>
            <label className="v3-field">
              <span>{t("videoManage.cdnUrl")} *</span>
              <input className="v3-input mono" type="url" value={draft.cdn_url} placeholder="https://cdn.example.com/media/video.mp4"
                aria-invalid={Boolean(draft.cdn_url && !cdnOk)} onChange={(e) => update("cdn_url", e.target.value)} />
              <small className={draft.cdn_url && !cdnOk ? "v3-err" : "v3-hint"}>
                {draft.cdn_url && !cdnOk ? t("videoManage.invalidCdn") : t("videoManage.cdnHint")}
              </small>
            </label>
            <details className="v3-vm-advanced">
              <summary>{t("videoManage.advanced")}</summary>
              <div className="v3-vm-form">
                <label className="v3-field">
                  <span>{t("videoManage.webmUrl")}</span>
                  <input className="v3-input mono" type="url" value={draft.webm_url ?? ""} onChange={(e) => update("webm_url", e.target.value || null)} />
                </label>
                <label className="v3-field">
                  <span>{t("videoManage.posterUrl")}</span>
                  <input className="v3-input mono" type="url" value={draft.poster_url ?? ""} onChange={(e) => update("poster_url", e.target.value || null)} />
                </label>
                <label className="v3-field">
                  <span>{t("videoManage.captionUrl")}</span>
                  <input className="v3-input mono" type="url" value={draft.caption_url ?? ""} onChange={(e) => update("caption_url", e.target.value || null)} />
                </label>
                <div className="v3-grid c2" style={{ alignItems: "start" }}>
                  <label className="v3-field">
                    <span>{t("videoManage.duration")}</span>
                    <input className="v3-input mono" type="number" min={0} step="0.001" value={draft.duration_seconds}
                      onChange={(e) => update("duration_seconds", Number(e.target.value))} />
                  </label>
                  <label className="v3-field">
                    <span>{t("videoManage.order")}</span>
                    <input className="v3-input mono" type="number" min={0} max={1000000} value={draft.sort_order}
                      onChange={(e) => update("sort_order", Number(e.target.value))} />
                  </label>
                </div>
                <label className="v3-field">
                  <span>{t("videoManage.chapters")}</span>
                  <textarea className="v3-input mono" rows={7} value={chaptersText} onChange={(e) => setChaptersText(e.target.value)} />
                  <small className="v3-hint">{t("videoManage.chaptersHint")}</small>
                </label>
              </div>
            </details>
          </fieldset>
          <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 16 }}>
            <Btn kind="primary" busy={busy} disabled={!canSave} disabledReason={saveReason ? t(saveReason) : null} onClick={() => void save()}>
              {t(busy ? "videoManage.saving" : "v3.videomgmt.save")}
            </Btn>
            {saveReason && !busy && <span style={{ color: "var(--v3-text-3)", fontSize: 13 }}>{t(saveReason)}</span>}
          </div>
        </Panel>
        <Panel title={t("videoManage.preview")} end={<span style={{ color: "var(--v3-text-3)" }}>{t("videoManage.previewHint")}</span>}>
          <div style={{ display: "grid", gap: 14 }}>
            {draft.cdn_url && cdnOk ? (
              <video controls preload="none" className="v3-vm-preview" poster={draft.poster_url ?? undefined} src={draft.cdn_url} />
            ) : (
              <p style={{ margin: 0, color: "var(--v3-text-3)" }}>{t("videoManage.noPreview")}</p>
            )}
            {row && (
              <dl className="v3-kv">
                <dt>ID</dt><dd className="mono">{row.id}</dd>
                <dt>{t("videoManage.revision")}</dt><dd className="mono">{row.revision}</dd>
                <dt>{t("videoManage.publishedAt")}</dt><dd>{row.published_at ? ago(row.published_at) : "—"}</dd>
              </dl>
            )}
            {row?.published_content && row.has_unpublished_changes && <Notice s="wait">{t("videoManage.publishedUnchanged")}</Notice>}
          </div>
        </Panel>
      </div>
      {confirm && (
        <Confirm
          title={t(`videoManage.confirm.${confirm}.title`)}
          confirmLabel={t(`videoManage.${confirm}`)}
          cancelLabel={t("v3.common.cancel")}
          danger={confirm === "delete" || confirm === "unpublish"}
          busy={busy}
          onCancel={() => setConfirm(null)}
          onConfirm={() => void act()}
        >
          {t(`videoManage.confirm.${confirm}.body`, { title: row?.content.title[locale] ?? "" })}
        </Confirm>
      )}
    </div>
  );
}

function Manager() {
  const [params, setParams] = useSearchParams();
  const catalog = useLoad(() => api.manageVideos(), "v3-video-management");
  const view = params.get("view");
  const selected = view === "new" ? NEW : view === "edit" ? params.get("id") : null;
  const back = useCallback(() => setParams({}), [setParams]);
  const { t } = useTranslation();
  if (selected) {
    if (!catalog.data) {
      return catalog.loading ? <Skeleton rows={6} /> : (
        <Notice s="act">{catalog.error} <Btn size="sm" onClick={catalog.reload}>{t("v3.videomgmt.retry")}</Btn></Notice>
      );
    }
    return (
      <Editor
        key={selected}
        id={selected}
        taxonomy={catalog.data.taxonomy}
        onBack={back}
        onCreated={(id) => setParams({ view: "edit", id }, { replace: true })}
        onChanged={catalog.reload}
      />
    );
  }
  return (
    <List
      catalog={catalog.data}
      loading={catalog.loading}
      error={catalog.error}
      onRetry={catalog.reload}
      onOpen={(id) => setParams(id === NEW ? { view: "new" } : { view: "edit", id })}
    />
  );
}

/** The hub-global video catalogue's editor (admin-only; the API enforces it too). */
export function V3VideoManagement() {
  const { isAdmin } = useAuth();
  const { t } = useTranslation();
  if (!isAdmin) {
    return (
      <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
        <PageHead eyebrow={t("v3.videomgmt.eyebrow")} title={t("videoManage.title")} sub={t("auth.adminRequired.meta")} />
        <Panel>
          <div className="v3-empty" data-testid="video-manage-forbidden">
            <ShieldAlert size={28} aria-hidden="true" />
            <strong>{t("auth.adminRequired.title")}</strong>
            {t("auth.adminRequired.body")}
          </div>
        </Panel>
      </div>
    );
  }
  return <Manager />;
}
