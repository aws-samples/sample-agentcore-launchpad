import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { AdminRequired } from "../components/AdminRequired";
import { AnnouncementContentView } from "../components/AnnouncementContentView";
import { AnnouncementPager } from "../components/AnnouncementPager";
import { Btn } from "../components/Btn";
import { Chip } from "../components/Chip";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { LoadError } from "../components/LoadError";
import { Panel } from "../components/Panel";
import { ViewHead } from "../components/ViewHead";
import {
  api, ApiError, errorMessage,
  type Announcement, type AnnouncementContent, type AnnouncementPage,
} from "../lib/api";
import { announcementLink, sameAnnouncementContent } from "../lib/announcements";
import "../components/announcements.css";

const PAGE_SIZE = 20;
const NEW_ID = "new";
const EMPTY: AnnouncementContent = { title: "", body: "", link_url: null, link_label: null };
type Action = "save" | "publish" | "unpublish" | "delete" | "reload";
interface DraftSession {
  row: Announcement | null;
  content: AnnouncementContent;
  busy: Action | null;
  error: string | null;
  errorCode: string | null;
  notice: string | null;
}

function sessionFor(row: Announcement | null): DraftSession {
  return { row, content: row?.content ?? { ...EMPTY }, busy: null, error: null,
    errorCode: null, notice: null };
}

function isDirty(session: DraftSession): boolean {
  return !sameAnnouncementContent(session.content, session.row?.content ?? EMPTY);
}

export function Announcements() {
  const { isAdmin } = useAuth();
  const { t } = useTranslation();
  // Mount no loaders or editor state for a member, including direct deep links.
  return isAdmin ? <AnnouncementManager /> : (
    <AdminRequired kicker={t("announcements.kicker")} title={t("announcements.title")}
      testId="announcements-forbidden" />
  );
}

function AnnouncementManager() {
  const { t, i18n } = useTranslation();
  const [params, setParams] = useSearchParams();
  const selected = params.get("announcement");
  const selectionRef = useRef(selected);
  selectionRef.current = selected;
  const paramsRef = useRef(params);
  paramsRef.current = params;
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [list, setList] = useState<(AnnouncementPage<Announcement> & { offset: number }) | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  // Local buffers are keyed by id. List refreshes and selection changes never
  // rehydrate a loaded buffer; only an explicit reload replaces the user's text.
  const [sessions, setSessions] = useState<Record<string, DraftSession>>({});
  const [detailError, setDetailError] = useState<{ id: string; message: string } | null>(null);
  const [detailRetry, setDetailRetry] = useState(0);
  const [confirmation, setConfirmation] = useState<{ id: string; action: Exclude<Action, "save"> } | null>(null);
  const inFlight = useRef(new Set<string>());
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const hasUnsaved = Object.values(sessions).some((session) => isDirty(session) || session.busy);
  useEffect(() => {
    if (!hasUnsaved) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [hasUnsaved]);

  useEffect(() => {
    const controller = new AbortController();
    setListLoading(true);
    setListError(null);
    api.manageAnnouncements(PAGE_SIZE, offset, controller.signal)
      .then((page) => {
        if (controller.signal.aborted) return;
        if (offset > 0 && offset >= page.total) {
          setOffset(Math.max(0, Math.floor((page.total - 1) / PAGE_SIZE) * PAGE_SIZE));
          return;
        }
        setList({ ...page, offset });
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setListError(errorMessage(err));
      })
      .finally(() => {
        if (!controller.signal.aborted) setListLoading(false);
      });
    return () => controller.abort();
  }, [offset, refresh]);

  const loaded = selected ? Boolean(sessions[selected]) : false;
  useEffect(() => {
    if (!selected || loaded) return;
    if (selected === NEW_ID) {
      setSessions((previous) => ({ ...previous, [NEW_ID]: sessionFor(null) }));
      return;
    }
    const controller = new AbortController();
    setDetailError(null);
    api.getAnnouncement(selected, controller.signal)
      .then((row) => {
        if (!controller.signal.aborted) {
          setSessions((previous) => ({ ...previous, [selected]: sessionFor(row) }));
        }
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setDetailError({ id: selected, message: errorMessage(err) });
      });
    return () => controller.abort();
  }, [selected, loaded, detailRetry]);

  const select = (id: string | null) => {
    const next = new URLSearchParams(paramsRef.current);
    if (id) next.set("announcement", id);
    else next.delete("announcement");
    setParams(next);
  };
  const updateSession = (id: string, update: Partial<DraftSession>) => {
    setSessions((previous) => previous[id]
      ? { ...previous, [id]: { ...previous[id], ...update } } : previous);
  };

  const act = async (id: string, action: Action) => {
    const session = sessions[id];
    if (!session || inFlight.current.has(id)) return;
    // Publication always targets the saved snapshot the operator reviewed.
    if (action === "publish" && isDirty(session)) return;
    inFlight.current.add(id);
    updateSession(id, { busy: action, error: null, errorCode: null, notice: null });
    try {
      let row: Announcement;
      if (action === "save") {
        const content = {
          title: session.content.title.trim(),
          body: session.content.body.trim(),
          link_url: session.content.link_url?.trim() || null,
          link_label: session.content.link_label?.trim() || null,
        };
        row = session.row
          ? await api.saveAnnouncement(id, content, session.row.revision)
          : await api.createAnnouncement(content);
      } else {
        if (!session.row) return;
        if (action === "delete") {
          await api.deleteAnnouncement(id, session.row.revision);
          if (!mounted.current) return;
          setSessions((previous) => {
            const next = { ...previous };
            delete next[id];
            return next;
          });
          if (selectionRef.current === id) select(null);
          setRefresh((v) => v + 1);
          return;
        }
        row = action === "publish" ? await api.publishAnnouncement(id, session.row.revision)
          : action === "unpublish" ? await api.unpublishAnnouncement(id, session.row.revision)
          : await api.getAnnouncement(id);
      }
      if (!mounted.current) return;
      setSessions((previous) => {
        const next = { ...previous };
        if (id === NEW_ID) delete next[NEW_ID];
        next[row.id] = {
          ...sessionFor(row),
          // Withdrawing changes publication only; keep any unsaved draft edits.
          content: action === "unpublish" ? session.content : row.content,
          notice: `announcements.success.${action}`,
        };
        return next;
      });
      if (id === NEW_ID && selectionRef.current === NEW_ID) select(row.id);
      setRefresh((v) => v + 1);
    } catch (err: unknown) {
      if (mounted.current) updateSession(id, {
        busy: null, error: errorMessage(err), errorCode: err instanceof ApiError ? err.code : null,
      });
    } finally {
      inFlight.current.delete(id);
    }
  };

  const current = list?.offset === offset ? list : null;
  const session = selected ? sessions[selected] : undefined;
  const formatDate = (value: string) => new Date(value).toLocaleString(i18n.language);
  const pending = confirmation ? sessions[confirmation.id] : undefined;
  const buffered = Object.entries(sessions).filter(([id, value]) =>
    id === NEW_ID || isDirty(value) || value.busy || value.error);

  return (
    <section data-testid="announcements-manager">
      <ViewHead kicker={t("announcements.kicker")} title={t("announcements.title")}
        meta={t("announcements.description")} />
      <div className="announcement-manager">
        <Panel title={t("announcements.listTitle")} pad={false}
          className="announcement-list"
          end={<Btn onClick={() => select(NEW_ID)} data-testid="announcement-new">
            + {t("announcements.new")}
          </Btn>}>
          {buffered.length > 0 ? (
            <div className="announcement-buffers">
              <span className="fhint">{t("announcements.buffers")}</span>
              {buffered.map(([id, value]) => (
                <button key={id} type="button" className="announcement-buffer"
                  aria-current={selected === id ? "true" : undefined} onClick={() => select(id)}>
                  {value.content.title || t("announcements.newDraft")}
                  {" · "}{t(value.busy ? "announcements.working" :
                    value.error ? "announcements.needsAttention" : "announcements.unsaved")}
                </button>
              ))}
            </div>
          ) : null}
          {listError ? <LoadError message={listError} onRetry={() => setRefresh((v) => v + 1)}
            data-testid="announcements-manage-error" /> : null}
          {listLoading ? <div className="loading-line" role="status">{t("common.loading")}</div> : null}
          {current?.announcements.map((row) => (
            <button key={row.id} type="button" className="announcement-list-row"
              onClick={() => select(row.id)} aria-current={selected === row.id ? "true" : undefined}
              data-testid={`announcement-select-${row.id}`}>
              <strong>{row.content.title}</strong>
              <span className="announcement-status">
                <Chip tone={row.status === "published" ? "good" : "muted"}>
                  {t(`announcements.status.${row.status}`)}
                </Chip>
                {row.status === "published" && row.has_unpublished_changes ? (
                  <Chip tone="warn">{t("announcements.unpublishedChanges")}</Chip>
                ) : null}
              </span>
              <time className="announcement-date" dateTime={row.updated_at}>
                {t("announcements.updated", { date: formatDate(row.updated_at) })}
              </time>
            </button>
          ))}
          {!listLoading && !listError && current?.total === 0 ? (
            <div className="empty">{t("announcements.manageEmpty")}</div>
          ) : null}
          <AnnouncementPager total={current?.total ?? list?.total ?? 0} offset={offset}
            limit={PAGE_SIZE} busy={listLoading} onOffset={setOffset} />
        </Panel>

        {selected && session ? (
          <AnnouncementEditor key={selected} session={session}
            onContent={(content) => updateSession(selected, { content, notice: null })}
            onSave={() => void act(selected, "save")}
            onAction={(action) => {
              if (action === "reload" && !isDirty(session)) void act(selected, action);
              else setConfirmation({ id: selected, action });
            }} />
        ) : selected && detailError?.id === selected ? (
          <Panel title={t("announcements.editorTitle")}>
            <LoadError message={detailError.message} onRetry={() => setDetailRetry((v) => v + 1)}
              data-testid="announcement-detail-error" />
            <Btn onClick={() => select(null)}>{t("announcements.clearSelection")}</Btn>
          </Panel>
        ) : (
          <Panel title={t("announcements.editorTitle")}>
            <div className={selected ? "loading-line" : "empty"} role={selected ? "status" : undefined}>
              {t(selected ? "common.loading" : "announcements.pick")}
            </div>
          </Panel>
        )}
      </div>
      <ConfirmDialog open={Boolean(confirmation && pending)}
        title={t(`announcements.confirm.${confirmation?.action ?? "delete"}.title`)}
        body={t(`announcements.confirm.${confirmation?.action ?? "delete"}.body`, {
          title: pending?.row?.content.title ?? pending?.content.title ?? "",
        })}
        confirmLabel={t(`announcements.actions.${confirmation?.action ?? "delete"}`)}
        onCancel={() => setConfirmation(null)}
        onConfirm={() => {
          const target = confirmation;
          setConfirmation(null);
          if (target) void act(target.id, target.action);
        }} />
    </section>
  );
}

function AnnouncementEditor({ session, onContent, onSave, onAction }: {
  session: DraftSession;
  onContent: (content: AnnouncementContent) => void;
  onSave: () => void;
  onAction: (action: Exclude<Action, "save">) => void;
}) {
  const { t, i18n } = useTranslation();
  const { row, content, busy, error, errorCode, notice } = session;
  const dirty = isDirty(session);
  const linkUrl = content.link_url?.trim() ?? "";
  const linkLabel = content.link_label?.trim() ?? "";
  const validation = !content.title.trim() || !content.body.trim()
    ? "announcements.validation.required"
    : Boolean(linkUrl) !== Boolean(linkLabel) ? "announcements.validation.linkPair"
    : linkUrl && !announcementLink(linkUrl) ? "announcements.validation.linkUrl" : null;
  const saveReason = validation ?? (!dirty && row ? "announcements.noChanges" : null);
  const publishReason = dirty ? "announcements.saveFirst"
    : !row ? "announcements.createFirst"
    : row.status === "published" && !row.has_unpublished_changes ? "announcements.alreadyPublished" : null;
  const formatDate = (value: string) => new Date(value).toLocaleString(i18n.language);
  return (
    <div className="announcement-editor" data-testid="announcement-editor" aria-busy={Boolean(busy)}>
      <Panel title={row ? t("announcements.editorTitle") : t("announcements.newDraft")}
        sub={row ? t("announcements.revision", { revision: row.revision }) : undefined}>
        <div className="announcement-status">
          <Chip tone={row?.status === "published" ? "good" : "muted"}>
            {t(`announcements.status.${row?.status ?? "draft"}`)}
          </Chip>
          {dirty ? <Chip tone="warn">{t("announcements.unsaved")}</Chip> : null}
          {row?.status === "published" && row.has_unpublished_changes ? (
            <Chip tone="warn">{t("announcements.unpublishedChanges")}</Chip>
          ) : null}
        </div>
        <p className="fhint">{t("announcements.draftHint")}</p>
        {row ? <p className="fhint">{t("announcements.editedBy", {
          user: row.updated_by, date: formatDate(row.updated_at),
        })}</p> : null}
        <form onSubmit={(event) => { event.preventDefault(); if (!saveReason && !busy) onSave(); }}>
          <fieldset disabled={Boolean(busy)} className="announcement-fields">
            <legend className="announcement-sr-only">{t("announcements.editorTitle")}</legend>
            <div className="field">
              <label htmlFor="announcement-title">{t("announcements.fields.title")}</label>
              <input id="announcement-title" name="title" className="input" required maxLength={160}
                value={content.title} onChange={(event) => onContent({ ...content, title: event.target.value })}
                data-testid="announcement-title" />
            </div>
            <div className="field">
              <label htmlFor="announcement-body">{t("announcements.fields.body")}</label>
              <textarea id="announcement-body" name="body" className="input" required maxLength={6000}
                rows={8} value={content.body} aria-describedby="announcement-content-hint"
                onChange={(event) => onContent({ ...content, body: event.target.value })}
                data-testid="announcement-body" />
              <span id="announcement-content-hint" className="fhint">{t("announcements.contentHint")}</span>
            </div>
            <div className="field">
              <label htmlFor="announcement-url">{t("announcements.fields.linkUrl")}</label>
              <input id="announcement-url" name="link_url" className="input" maxLength={2048}
                value={content.link_url ?? ""} aria-describedby="announcement-link-hint"
                spellCheck={false} onChange={(event) => onContent({ ...content, link_url: event.target.value })}
                data-testid="announcement-url" />
              <span id="announcement-link-hint" className="fhint">{t("announcements.linkHint")}</span>
            </div>
            <div className="field">
              <label htmlFor="announcement-link-label">{t("announcements.fields.linkLabel")}</label>
              <input id="announcement-link-label" name="link_label" className="input" maxLength={80}
                value={content.link_label ?? ""}
                onChange={(event) => onContent({ ...content, link_label: event.target.value })}
                data-testid="announcement-link-label" />
            </div>
          </fieldset>
          {error ? (
            <div className="announcement-error" role="alert" data-testid="announcement-action-error">
              <p>{error}</p>
              <p>{t(errorCode === "announcements.conflict" ?
                "announcements.conflictHint" : "announcements.preserved")}</p>
            </div>
          ) : null}
          {notice ? <p className="announcement-success" role="status">{t(notice)}</p> : null}
          <div className="announcement-save">
            <Btn type="submit" primary disabled={Boolean(busy || saveReason)}
              disabledReason={!busy && saveReason ? t(saveReason) : undefined}
              data-testid="announcement-save">
              {t(busy === "save" ? "announcements.saving" : "announcements.actions.save")}
            </Btn>
          </div>
        </form>
        {row ? (
          <div className="announcement-actions">
            <div className="announcement-publish">
              <Btn primary disabled={Boolean(busy || publishReason)}
                disabledReason={!busy && publishReason ? t(publishReason) : undefined}
                onClick={() => onAction("publish")} data-testid="announcement-publish">
                {t(busy === "publish" ? "announcements.publishing" : "announcements.actions.publish")}
              </Btn>
            </div>
            {row.status === "published" ? (
              <Btn disabled={Boolean(busy)} onClick={() => onAction("unpublish")}
                data-testid="announcement-unpublish">
                {t("announcements.actions.unpublish")}
              </Btn>
            ) : null}
            <Btn disabled={Boolean(busy)} onClick={() => onAction("reload")}
              data-testid="announcement-reload">{t("announcements.actions.reload")}</Btn>
            <Btn disabled={Boolean(busy)} className="announcement-delete" onClick={() => onAction("delete")}
              data-testid="announcement-delete">{t("announcements.actions.delete")}</Btn>
          </div>
        ) : null}
      </Panel>
      <Panel title={t("announcements.publishedTitle")}
        sub={row?.published_at ? t("announcements.publishedAt", {
          date: formatDate(row.published_at),
        }) : undefined}
        className="announcement-published" data-testid="announcement-published-snapshot">
        {row?.published_content ? (
          <>
            <p className="fhint">{t("announcements.publishedHint")}</p>
            <AnnouncementContentView content={row.published_content} />
          </>
        ) : <p className="dim">{t("announcements.notPublic")}</p>}
      </Panel>
    </div>
  );
}
