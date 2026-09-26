import { Plus, ShieldAlert } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { api, ApiError, errorMessage } from "../../lib/api";
import type { ConsoleVersion, ManagedVideo, ManagedVideoCatalog, VideoContent, VideoLocale, VideoTaxonomy } from "../../lib/videos";
import { fmtTime } from "../format";
import { useLoad, useV2Toast } from "../hooks";
import { Alert, Button, Card, type Column, Confirm, Field, FlowHeader, LinkButton, PageHeader, Spin, Table, Tag } from "../ui";
import "./video-management/video-management.css";

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

function directoryLabel(taxonomy: VideoTaxonomy, content: VideoContent, locale: VideoLocale): string {
  const category = taxonomy.categories.find((item) => item.id === content.category_id);
  const section = taxonomy.sections.find((item) => item.id === content.section_id);
  return [category?.title[locale], section?.title[locale]].filter(Boolean).join(" / ");
}

/** Admin-only boundary is above this component; the API independently enforces it. */
function VideoList({
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
  const { t, i18n } = useTranslation();
  const locale: VideoLocale = i18n.resolvedLanguage?.startsWith("zh") ? "zh-CN" : "en";
  const rows = catalog?.videos ?? [];
  const published = rows.filter((row) => row.status === "published").length;
  const columns: Column<ManagedVideo>[] = [
    {
      key: "title", title: t("videoManage.titleField"),
      render: (row) => (
        <>
          <LinkButton onClick={() => onOpen(row.id)} testId={`video-manage-open-${row.id}`}>
            {row.content.title[locale]}
          </LinkButton>
          <span className="sub mono">ID: {row.id}</span>
        </>
      ),
    },
    {
      key: "directory", title: t("videoManage.directory"),
      render: (row) => catalog ? directoryLabel(catalog.taxonomy, row.content, locale) : "—",
    },
    {
      key: "version", title: t("videoManage.consoleVersion"),
      render: (row) => {
        const version = row.published_content?.console_version ?? row.content.console_version;
        return <Tag tone={version === "v2" ? "blue" : "gray"}>{t(`videos.version.${version}`)}</Tag>;
      },
    },
    {
      key: "status", title: t("videoManage.status"),
      render: (row) => (
        <span className="v2-row">
          <Tag tone={row.status === "published" ? "green" : "orange"}>
            {t(`videoManage.${row.status}`)}
          </Tag>
          {row.status === "published" && row.has_unpublished_changes && (
            <Tag tone="blue">{t("videoManage.unpublishedChanges")}</Tag>
          )}
        </span>
      ),
    },
    { key: "updated", title: t("videoManage.updated"), render: (row) => fmtTime(row.updated_at) },
    {
      key: "actions", title: t("v2.common.actions"), className: "right",
      render: (row) => <LinkButton onClick={() => onOpen(row.id)}>{t("v2.common.edit")}</LinkButton>,
    },
  ];
  return (
    <>
      <PageHeader title={t("videoManage.title")} desc={t("videoManage.description")} />
      {error && catalog && <Alert tone="error" action={<Button onClick={onRetry}>{t("v2.common.retry")}</Button>}>{error}</Alert>}
      <Card>
        <div className="v2-toolbar">
          <Button onClick={onRetry}>{t("v2.common.refresh")}</Button>
          <Button kind="primary" onClick={() => onOpen(NEW)} testId="video-manage-new">
            <Plus size={14} aria-hidden="true" /> {t("videoManage.new")}
          </Button>
          <div className="end">
            <span className="v2-count">{t("videoManage.counts", { total: rows.length, published })}</span>
          </div>
        </div>
        <Table
          columns={columns}
          rows={rows}
          rowKey={(row) => row.id}
          loading={loading}
          error={catalog ? null : error}
          onRetry={onRetry}
          empty={t("videoManage.empty")}
          testId="video-manage-table"
        />
      </Card>
    </>
  );
}

function VideoEditor({
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
  const { t, i18n } = useTranslation();
  const toast = useV2Toast();
  const locale: VideoLocale = i18n.resolvedLanguage?.startsWith("zh") ? "zh-CN" : "en";
  const [row, setRow] = useState<ManagedVideo | null>(null);
  const [draft, setDraft] = useState<VideoContent>(emptyVideo);
  const [chaptersText, setChaptersText] = useState("[]");
  const [loading, setLoading] = useState(id !== NEW);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Action | null>(null);

  const load = useCallback(async () => {
    if (id === NEW) { setLoading(false); return; }
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
  useEffect(() => { void load(); }, [load]);

  const update = <K extends keyof VideoContent>(field: K, value: VideoContent[K]) =>
    setDraft((prev) => ({ ...prev, [field]: value }));
  const updateText = (field: "title" | "description", language: VideoLocale, value: string) =>
    setDraft((prev) => ({ ...prev, [field]: { ...prev[field], [language]: value } }));

  const directoryOk = taxonomy.sections.some((section) =>
    section.id === draft.section_id && section.categoryId === draft.category_id);
  const titleOk = Boolean(draft.title["zh-CN"].trim());
  const descriptionOk = Boolean(draft.description["zh-CN"].trim());
  const cdnOk = /^https:\/\/[^?#]+\.(mp4|webm)$/i.test(draft.cdn_url.trim());
  const dirty = !row || JSON.stringify(draft) !== JSON.stringify(row.content) ||
    chaptersText !== JSON.stringify(row.content.chapters, null, 2);
  const saveReason = !draft.category_id ? "videoManage.chooseCategory"
    : !directoryOk ? "videoManage.chooseSection"
      : !titleOk ? "videoManage.titleRequired"
        : !descriptionOk ? "videoManage.descriptionRequired"
          : !cdnOk ? "videoManage.invalidCdn"
            : !dirty ? "videoManage.noChanges" : null;
  const publishReason = !row || dirty ? "videoManage.saveFirst"
    : row.status === "published" && !row.has_unpublished_changes ? "videoManage.alreadyPublished" : null;
  const canSave = !busy && !saveReason;
  const canPublish = !busy && !publishReason;
  const sections = taxonomy.sections.filter((section) => section.categoryId === draft.category_id);

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
      const saved = row
        ? await api.saveVideo(row.id, content, row.revision)
        : await api.createVideo(content);
      setRow(saved);
      setDraft(saved.content);
      setChaptersText(JSON.stringify(saved.content.chapters, null, 2));
      setError(null);
      toast("success", t("videoManage.saved"));
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
        const next = confirm === "publish"
          ? await api.publishVideo(row.id, row.revision)
          : await api.unpublishVideo(row.id, row.revision);
        setRow(next);
        setDraft(next.content);
        setChaptersText(JSON.stringify(next.content.chapters, null, 2));
        onChanged();
      }
      setError(null);
      toast("success", t(`videoManage.${confirm}Done`));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  if (loading && !row) return <Spin />;
  if (id !== NEW && !row) {
    return (
      <>
        <FlowHeader title={t("videoManage.title")} onBack={onBack} />
        <Alert tone="error" action={<Button onClick={() => void load()}>{t("v2.common.retry")}</Button>}>
          {error ?? t("videoManage.notFound")}
        </Alert>
      </>
    );
  }

  return (
    <div data-testid="video-manage-editor" aria-busy={busy}>
      <FlowHeader
        title={row ? draft.title[locale] || t("videoManage.edit") : t("videoManage.new")}
        onBack={onBack}
        end={
          <div className="v2-row">
            {row && <Tag tone={row.status === "published" ? "green" : "orange"}>{t(`videoManage.${row.status}`)}</Tag>}
            {row && <Button onClick={() => void load()} disabled={busy}>{t("v2.common.refresh")}</Button>}
            {row && row.status === "published" && (
              <Button onClick={() => setConfirm("unpublish")} disabled={busy} testId="video-manage-unpublish">
                {t("videoManage.unpublish")}
              </Button>
            )}
            {row && (
              <Button kind="danger" onClick={() => setConfirm("delete")} disabled={busy} testId="video-manage-delete">
                {t("v2.common.delete")}
              </Button>
            )}
            <Button kind="primary" onClick={() => setConfirm("publish")} disabled={!canPublish}
              title={publishReason ? t(publishReason) : undefined} testId="video-manage-publish">
              {t("videoManage.publish")}
            </Button>
          </div>
        }
      />
      {error && <Alert tone="error" action={row ? <Button onClick={() => void load()}>{t("videoManage.reload")}</Button> : undefined}>{error}</Alert>}
      <Alert>{t("videoManage.draftHint")}</Alert>
      <div className="v2-video-manage-layout">
        <Card title={t("videoManage.edit")}>
          <fieldset className="v2-video-manage-form v2-form" disabled={busy}>
            <Field label={t("videoManage.consoleVersion")} required>
              <select className="v2-select" value={draft.console_version}
                onChange={(event) => update("console_version", event.target.value as ConsoleVersion)}
                data-testid="video-manage-version">
                <option value="v2">{t("videos.version.v2")}</option>
                <option value="classic">{t("videos.version.classic")}</option>
              </select>
            </Field>
            <div className="v2-form cols-2">
              <Field label={t("videoManage.category")} required>
                <select className="v2-select" value={draft.category_id}
                  onChange={(event) => setDraft((prev) => ({ ...prev, category_id: event.target.value, section_id: "" }))}
                  data-testid="video-manage-category">
                  <option value="">{t("videoManage.chooseCategory")}</option>
                  {taxonomy.categories.map((category) => (
                    <option key={category.id} value={category.id}>{category.title[locale]}</option>
                  ))}
                </select>
              </Field>
              <Field label={t("videoManage.section")} required>
                <select className="v2-select" value={draft.section_id} disabled={!draft.category_id}
                  onChange={(event) => update("section_id", event.target.value)}
                  data-testid="video-manage-section">
                  <option value="">{t("videoManage.chooseSection")}</option>
                  {sections.map((section) => (
                    <option key={section.id} value={section.id}>{section.title[locale]}</option>
                  ))}
                </select>
              </Field>
            </div>
            <div className="v2-form cols-2">
              <Field label={t("videoManage.titleZh")} required>
                <input className="v2-input" value={draft.title["zh-CN"]}
                  onChange={(event) => updateText("title", "zh-CN", event.target.value)}
                  maxLength={160} data-testid="video-manage-title" />
              </Field>
              <Field label={t("videoManage.titleEn")} hint={t("videoManage.englishFallback")}>
                <input className="v2-input" value={draft.title.en}
                  onChange={(event) => updateText("title", "en", event.target.value)} maxLength={160} />
              </Field>
            </div>
            <div className="v2-form cols-2">
              <Field label={t("videoManage.descriptionZh")} required>
                <textarea className="v2-input" value={draft.description["zh-CN"]}
                  onChange={(event) => updateText("description", "zh-CN", event.target.value)}
                  maxLength={2000} rows={4} data-testid="video-manage-description" />
              </Field>
              <Field label={t("videoManage.descriptionEn")} hint={t("videoManage.englishFallback")}>
                <textarea className="v2-input" value={draft.description.en}
                  onChange={(event) => updateText("description", "en", event.target.value)} maxLength={2000} rows={4} />
              </Field>
            </div>
            <Field label={t("videoManage.cdnUrl")} required hint={t("videoManage.cdnHint")}
              error={draft.cdn_url && !cdnOk ? t("videoManage.invalidCdn") : null}>
              <input className="v2-input mono" type="url" value={draft.cdn_url}
                onChange={(event) => update("cdn_url", event.target.value)}
                placeholder="https://cdn.example.com/media/video.mp4" data-testid="video-manage-cdn" />
            </Field>
            <details className="v2-video-manage-advanced">
              <summary>{t("videoManage.advanced")}</summary>
              <div className="v2-form">
                <Field label={t("videoManage.webmUrl")}>
                  <input className="v2-input mono" type="url" value={draft.webm_url ?? ""}
                    onChange={(event) => update("webm_url", event.target.value || null)} />
                </Field>
                <Field label={t("videoManage.posterUrl")}>
                  <input className="v2-input mono" type="url" value={draft.poster_url ?? ""}
                    onChange={(event) => update("poster_url", event.target.value || null)} />
                </Field>
                <Field label={t("videoManage.captionUrl")}>
                  <input className="v2-input mono" type="url" value={draft.caption_url ?? ""}
                    onChange={(event) => update("caption_url", event.target.value || null)} />
                </Field>
                <div className="v2-form cols-2">
                  <Field label={t("videoManage.duration")}>
                    <input className="v2-input mono" type="number" min={0} step="0.001"
                      value={draft.duration_seconds}
                      onChange={(event) => update("duration_seconds", Number(event.target.value))} />
                  </Field>
                  <Field label={t("videoManage.order")}>
                    <input className="v2-input mono" type="number" min={0} max={1000000}
                      value={draft.sort_order}
                      onChange={(event) => update("sort_order", Number(event.target.value))} />
                  </Field>
                </div>
                <Field label={t("videoManage.chapters")} hint={t("videoManage.chaptersHint")}>
                  <textarea className="v2-input mono" value={chaptersText} rows={7}
                    onChange={(event) => setChaptersText(event.target.value)} />
                </Field>
              </div>
            </details>
          </fieldset>
          <div className="v2-row" style={{ marginTop: 16 }}>
            <Button kind="primary" disabled={!canSave} title={saveReason ? t(saveReason) : undefined}
              onClick={() => void save()} testId="video-manage-save">
              {t(busy ? "videoManage.saving" : "v2.common.save")}
            </Button>
            {saveReason && !busy && <span className="v2-muted">{t(saveReason)}</span>}
          </div>
        </Card>
        <Card title={t("videoManage.preview")} sub={t("videoManage.previewHint")}>
          {draft.cdn_url && cdnOk ? (
            <video controls preload="none" className="v2-video-manage-preview"
              poster={draft.poster_url ?? undefined} src={draft.cdn_url} data-testid="video-manage-preview" />
          ) : <div className="v2-muted">{t("videoManage.noPreview")}</div>}
          {row && (
            <dl className="v2-video-manage-meta">
              <div><dt>ID</dt><dd className="mono">{row.id}</dd></div>
              <div><dt>{t("videoManage.revision")}</dt><dd>{row.revision}</dd></div>
              <div><dt>{t("videoManage.publishedAt")}</dt><dd>{fmtTime(row.published_at)}</dd></div>
            </dl>
          )}
          {row?.published_content && row.has_unpublished_changes && (
            <Alert tone="warn">{t("videoManage.publishedUnchanged")}</Alert>
          )}
        </Card>
      </div>
      <Confirm
        open={confirm !== null}
        title={t(`videoManage.confirm.${confirm ?? "publish"}.title`)}
        body={t(`videoManage.confirm.${confirm ?? "publish"}.body`, { title: row?.content.title[locale] ?? "" })}
        confirmLabel={t(`videoManage.${confirm ?? "publish"}`)}
        danger={confirm === "delete" || confirm === "unpublish"}
        busy={busy}
        onConfirm={() => void act()}
        onClose={() => setConfirm(null)}
      />
    </div>
  );
}

export function V2VideoManagement() {
  const { isAdmin } = useAuth();
  const { t } = useTranslation();
  if (!isAdmin) {
    return (
      <>
        <PageHeader title={t("videoManage.title")} desc={t("auth.adminRequired.meta")} />
        <Card title={t("auth.adminRequired.title")}>
          <div className="v2-table-empty" data-testid="video-manage-forbidden">
            <ShieldAlert size={28} aria-hidden="true" />
            <div>{t("auth.adminRequired.body")}</div>
          </div>
        </Card>
      </>
    );
  }
  return <Manager />;
}

function Manager() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const catalog = useLoad(() => api.manageVideos(), "video-management");
  const view = params.get("view");
  const selected = view === "new" ? NEW : view === "edit" ? params.get("id") : null;
  const back = useCallback(() => setParams({}), [setParams]);
  if (selected) {
    if (!catalog.data) {
      return catalog.loading ? <Spin /> : (
        <Alert tone="error" action={<Button onClick={catalog.reload}>{t("v2.common.retry")}</Button>}>
          {catalog.error}
        </Alert>
      );
    }
    return (
      <VideoEditor
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
    <VideoList
      catalog={catalog.data}
      loading={catalog.loading}
      error={catalog.error}
      onRetry={catalog.reload}
      onOpen={(id) => setParams(id === NEW ? { view: "new" } : { view: "edit", id })}
    />
  );
}
