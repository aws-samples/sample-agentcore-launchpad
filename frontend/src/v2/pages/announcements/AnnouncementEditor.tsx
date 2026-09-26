import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { AnnouncementContent } from "../../../lib/api";
import { fmtTime } from "../../format";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, Descriptions, Field, FlowHeader, LinkButton, Spin } from "../../ui";
import {
  type ConfirmAction, isDirty, NEW_ID, publishReason, saveReason, validationKey,
} from "./common";
import { AnnouncementPreview } from "./Preview";
import type { AnnouncementSessions } from "./useSessions";
import { StatusTags } from "./widgets";

/**
 * 编辑公告 (`?view=edit&id=` / `?view=new`). Saving only updates the draft; publishing
 * promotes the saved draft to the version every user sees on the overview page.
 */
export function AnnouncementEditor({
  id,
  store,
  onBack,
  onMoved,
}: {
  id: string;
  store: AnnouncementSessions;
  onBack: () => void;
  /** The buffer `from` became `to` (created) or was deleted (null); the router follows only if still shown. */
  onMoved: (from: string, to: string | null) => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { sessions, detailError, ensure, setContent, act } = store;
  const session = sessions[id];
  const [confirm, setConfirm] = useState<ConfirmAction | null>(null);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    ensure(id, controller.signal);
    return () => controller.abort();
  }, [id, ensure, retry]);

  const run = async (action: "save" | ConfirmAction) => {
    const outcome = await act(id, action);
    if (!outcome?.ok) return; // failures stay inline with the preserved buffer
    if (action === "delete") {
      toast("success", t("v2.announcements.deleted"));
      onMoved(id, null);
      return;
    }
    toast("success", t(`announcements.success.${action}`));
    if (id === NEW_ID && outcome.row) onMoved(id, outcome.row.id);
  };

  const newTitle = t("announcements.newDraft");
  if (!session) {
    return (
      <>
        <FlowHeader title={id === NEW_ID ? newTitle : t("announcements.editorTitle")} onBack={onBack} />
        {detailError?.id === id ? (
          <Alert
            tone="error"
            action={<LinkButton onClick={() => setRetry((v) => v + 1)}>{t("v2.common.retry")}</LinkButton>}
          >
            <span data-testid="announcement-detail-error">{detailError.message}</span>
          </Alert>
        ) : (
          <Spin />
        )}
      </>
    );
  }

  const { row, content, busy, error, errorCode } = session;
  const dirty = isDirty(session);
  const invalid = dirty ? validationKey(content) : null;
  const cantSave = saveReason(session);
  const cantPublish = publishReason(session);
  const edit = (patch: Partial<AnnouncementContent>) => setContent(id, { ...content, ...patch });
  const linkUrlError =
    invalid === "announcements.validation.linkUrl" ||
    (invalid === "announcements.validation.linkPair" && !content.link_url?.trim())
      ? t(invalid)
      : null;
  const linkLabelError =
    invalid === "announcements.validation.linkPair" && !content.link_label?.trim() ? t(invalid) : null;
  // Save leads while there is something to save; afterwards publishing is the next step.
  const saveLeads = !cantSave || !row;

  return (
    <div data-testid="announcement-editor" aria-busy={Boolean(busy)}>
      <FlowHeader
        onBack={onBack}
        title={
          <span className="v2-row">
            {row?.content.title || newTitle}
            <StatusTags row={row} dirty={dirty} />
          </span>
        }
        end={
          <>
            {row && (
              <>
                <Button
                  disabled={Boolean(busy)}
                  onClick={() => (dirty ? setConfirm("reload") : void run("reload"))}
                  testId="announcement-reload"
                >
                  {t("announcements.actions.reload")}
                </Button>
                <Button kind="danger" disabled={Boolean(busy)} onClick={() => setConfirm("delete")} testId="announcement-delete">
                  {t("v2.common.delete")}
                </Button>
                {row.status === "published" && (
                  <Button disabled={Boolean(busy)} onClick={() => setConfirm("unpublish")} testId="announcement-unpublish">
                    {t("announcements.actions.unpublish")}
                  </Button>
                )}
                <Button
                  kind={saveLeads ? undefined : "primary"}
                  disabled={Boolean(busy || cantPublish)}
                  title={!busy && cantPublish ? t(cantPublish) : undefined}
                  onClick={() => setConfirm("publish")}
                  testId="announcement-publish"
                >
                  {t(busy === "publish" ? "announcements.publishing" : "announcements.actions.publish")}
                </Button>
              </>
            )}
            <Button
              kind={saveLeads ? "primary" : undefined}
              disabled={Boolean(busy || cantSave)}
              title={!busy && cantSave ? t(cantSave) : undefined}
              onClick={() => void run("save")}
              testId="announcement-save"
            >
              {t(busy === "save" ? "announcements.saving" : "announcements.actions.save")}
            </Button>
          </>
        }
      />

      {error && (
        <Alert tone="error">
          <span data-testid="announcement-action-error">
            {error}
            <br />
            {t(errorCode === "announcements.conflict" ? "announcements.conflictHint" : "announcements.preserved")}
          </span>
        </Alert>
      )}
      {invalid && invalid !== "announcements.validation.linkPair" && invalid !== "announcements.validation.linkUrl" && (
        <Alert tone="warn">{t(invalid)}</Alert>
      )}
      <Alert>{t("announcements.draftHint")}</Alert>

      <div className="v2-announcements-layout">
        <Card title={t("v2.announcements.draft")} sub={row ? t("announcements.revision", { revision: row.revision }) : undefined}>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              if (!cantSave && !busy) void run("save");
            }}
          >
            <fieldset className="v2-announcements-fieldset v2-form" disabled={Boolean(busy)}>
              <Field label={t("v2.announcements.field.title")} required hint={t("v2.announcements.hint.title", { count: content.title.length })}>
                <input
                  className="v2-input"
                  required
                  maxLength={160}
                  value={content.title}
                  onChange={(e) => edit({ title: e.target.value })}
                  data-testid="announcement-title"
                />
              </Field>
              <Field label={t("v2.announcements.field.body")} required hint={t("announcements.contentHint")}>
                <textarea
                  className="v2-textarea"
                  required
                  maxLength={6000}
                  rows={10}
                  value={content.body}
                  onChange={(e) => edit({ body: e.target.value })}
                  data-testid="announcement-body"
                />
              </Field>
              <div className="v2-form cols-2">
                <Field label={t("v2.announcements.field.linkUrl")} hint={t("announcements.linkHint")} error={linkUrlError}>
                  <input
                    className="v2-input mono"
                    maxLength={2048}
                    spellCheck={false}
                    placeholder="/v2/videos · https://…"
                    value={content.link_url ?? ""}
                    onChange={(e) => edit({ link_url: e.target.value })}
                    data-testid="announcement-url"
                  />
                </Field>
                <Field label={t("v2.announcements.field.linkLabel")} hint={t("v2.announcements.hint.linkLabel")} error={linkLabelError}>
                  <input
                    className="v2-input"
                    maxLength={80}
                    value={content.link_label ?? ""}
                    onChange={(e) => edit({ link_label: e.target.value })}
                    data-testid="announcement-link-label"
                  />
                </Field>
              </div>
            </fieldset>
            <button type="submit" hidden aria-hidden="true" tabIndex={-1} />
          </form>
        </Card>

        <div className="v2-announcements-side">
          <Card title={t("v2.announcements.draftPreview")} sub={t("v2.announcements.draftPreviewSub")}>
            <AnnouncementPreview content={content} testId="announcement-draft-preview" />
          </Card>
          <Card
            title={t("announcements.publishedTitle")}
            sub={row?.published_at ? t("announcements.publishedAt", { date: fmtTime(row.published_at) }) : undefined}
            testId="announcement-published-snapshot"
          >
            {row?.published_content ? (
              <>
                <div className="v2-announcements-note">{t("announcements.publishedHint")}</div>
                <AnnouncementPreview content={row.published_content} />
              </>
            ) : (
              <div className="v2-muted">{t("announcements.notPublic")}</div>
            )}
          </Card>
          {row && (
            <Card title={t("v2.announcements.info")}>
              <Descriptions
                one
                items={[
                  { label: "ID", value: <span className="mono">{row.id}</span> },
                  { label: t("v2.announcements.col.revision"), value: <span className="mono">{row.revision}</span> },
                  { label: t("v2.announcements.createdBy"), value: `${row.created_by} · ${fmtTime(row.created_at)}` },
                  { label: t("v2.announcements.updatedBy"), value: `${row.updated_by} · ${fmtTime(row.updated_at)}` },
                  { label: t("v2.announcements.col.published"), value: fmtTime(row.published_at) },
                ]}
              />
            </Card>
          )}
        </div>
      </div>

      <Confirm
        open={Boolean(confirm)}
        title={t(`announcements.confirm.${confirm ?? "delete"}.title`)}
        body={t(`announcements.confirm.${confirm ?? "delete"}.body`, { title: row?.content.title ?? content.title })}
        confirmLabel={t(`announcements.actions.${confirm ?? "delete"}`)}
        danger={confirm === "delete" || confirm === "reload"}
        onClose={() => setConfirm(null)}
        onConfirm={() => {
          const target = confirm;
          setConfirm(null);
          if (target) void run(target);
        }}
      />
    </div>
  );
}
