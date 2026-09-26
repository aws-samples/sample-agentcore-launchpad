import { Plus, RotateCw } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type Announcement } from "../../../lib/api";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, type Column, Confirm, LinkButton, PageHeader, Pager, Table } from "../../ui";
import { type ConfirmAction, isDirty, NEW_ID, PAGE_SIZE, publishReason, sessionFor } from "./common";
import type { AnnouncementSessions } from "./useSessions";
import { StatusTags } from "./widgets";

/** 公告列表 — server-paged (20 per page) list of every announcement, drafts included. */
export function AnnouncementList({
  store,
  offset,
  onOffset,
  onOpen,
}: {
  store: AnnouncementSessions;
  offset: number;
  onOffset: (offset: number) => void;
  onOpen: (id: string) => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { sessions, act, tick, refresh } = store;
  const list = useLoad(() => api.manageAnnouncements(PAGE_SIZE, offset), `${offset}:${tick}`);
  const total = list.data?.total ?? 0;
  const [confirm, setConfirm] = useState<{ row: Announcement; action: ConfirmAction } | null>(null);

  // A delete elsewhere can leave this page past the end — step back to the last page.
  useEffect(() => {
    if (list.data && offset > 0 && offset >= list.data.total) {
      onOffset(Math.max(0, Math.floor((list.data.total - 1) / PAGE_SIZE) * PAGE_SIZE));
    }
  }, [list.data, offset, onOffset]);

  const buffered = Object.entries(sessions).filter(
    ([id, s]) => id === NEW_ID || isDirty(s) || s.busy || s.error,
  );

  const run = async (row: Announcement, action: ConfirmAction) => {
    const outcome = await act(row.id, action, row);
    if (!outcome) return;
    if (outcome.ok) toast("success", t(action === "delete" ? "v2.announcements.deleted" : `announcements.success.${action}`));
    else toast("error", `${t(`announcements.actions.${action}`)}: ${outcome.error}`);
  };

  const columns: Column<Announcement>[] = [
    {
      key: "title",
      title: t("v2.announcements.col.title"),
      render: (row) => (
        <>
          <LinkButton onClick={() => onOpen(row.id)} testId={`announcement-select-${row.id}`}>
            {row.content.title}
          </LinkButton>
          <span className="sub mono">ID: {row.id}</span>
        </>
      ),
    },
    {
      key: "status",
      title: t("v2.announcements.col.status"),
      render: (row) => {
        const session = sessions[row.id];
        return <StatusTags row={row} dirty={session ? isDirty(session) : false} />;
      },
    },
    { key: "revision", title: t("v2.announcements.col.revision"), render: (row) => <span className="mono">{row.revision}</span> },
    {
      key: "updated",
      title: t("v2.announcements.col.updated"),
      render: (row) => (
        <>
          {fmtTime(row.updated_at)}
          <span className="sub">{row.updated_by}</span>
        </>
      ),
    },
    { key: "published", title: t("v2.announcements.col.published"), render: (row) => fmtTime(row.published_at) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (row) => {
        const session = sessions[row.id] ?? sessionFor(row);
        const busy = Boolean(session.busy);
        const blocked = publishReason(session);
        return (
          <div className="v2-actions">
            <LinkButton onClick={() => onOpen(row.id)}>{t("v2.common.edit")}</LinkButton>
            <LinkButton
              disabled={busy || Boolean(blocked)}
              title={blocked ? t(blocked) : undefined}
              onClick={() => setConfirm({ row, action: "publish" })}
              testId={`announcement-row-publish-${row.id}`}
            >
              {t("v2.announcements.publish")}
            </LinkButton>
            {row.status === "published" && (
              <LinkButton disabled={busy} onClick={() => setConfirm({ row, action: "unpublish" })}>
                {t("v2.announcements.unpublish")}
              </LinkButton>
            )}
            <LinkButton danger disabled={busy} onClick={() => setConfirm({ row, action: "delete" })}>
              {t("v2.common.delete")}
            </LinkButton>
          </div>
        );
      },
    },
  ];

  return (
    <>
      <PageHeader title={t("announcements.title")} desc={t("announcements.description")} />
      {buffered.length > 0 && (
        <Alert tone="warn">
          <span data-testid="announcement-buffers">
            {t("announcements.buffers")}
            <span className="v2-announcements-buffers">
              {buffered.map(([id, s]) => (
                <LinkButton key={id} onClick={() => onOpen(id)}>
                  {s.content.title || t("announcements.newDraft")} ·{" "}
                  {t(s.busy ? "announcements.working" : s.error ? "announcements.needsAttention" : "announcements.unsaved")}
                </LinkButton>
              ))}
            </span>
          </span>
        </Alert>
      )}
      <Card>
        <div className="v2-toolbar">
          <Button onClick={refresh}>
            <RotateCw size={14} aria-hidden="true" />
            {t("v2.common.refresh")}
          </Button>
          <Button kind="primary" onClick={() => onOpen(NEW_ID)} testId="announcement-new">
            <Plus size={14} aria-hidden="true" />
            {t("announcements.new")}
          </Button>
          <div className="end">
            <span className="v2-count">{t("v2.common.total", { count: total })}</span>
          </div>
        </div>
        <Table
          columns={columns}
          rows={list.data?.announcements ?? []}
          rowKey={(row) => row.id}
          loading={list.loading}
          error={list.error}
          onRetry={list.reload}
          empty={t("announcements.manageEmpty")}
          testId="announcements-table"
        />
        {total > PAGE_SIZE && (
          <Pager
            page={Math.floor(offset / PAGE_SIZE) + 1}
            pages={Math.max(1, Math.ceil(total / PAGE_SIZE))}
            total={total}
            onPage={(page) => onOffset((page - 1) * PAGE_SIZE)}
          />
        )}
      </Card>
      <Confirm
        open={Boolean(confirm)}
        title={t(`announcements.confirm.${confirm?.action ?? "delete"}.title`)}
        body={t(`announcements.confirm.${confirm?.action ?? "delete"}.body`, {
          title: confirm?.row.content.title ?? "",
        })}
        confirmLabel={t(`announcements.actions.${confirm?.action ?? "delete"}`)}
        danger={confirm?.action === "delete"}
        onClose={() => setConfirm(null)}
        onConfirm={() => {
          const target = confirm;
          setConfirm(null);
          if (target) void run(target.row, target.action);
        }}
      />
    </>
  );
}
