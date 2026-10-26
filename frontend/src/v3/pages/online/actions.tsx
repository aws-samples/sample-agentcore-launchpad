import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { api, type OnlineEvalConfigRow } from "../../../lib/api";
import { configName } from "../../../v2/online";
import { useToast } from "../../hooks";
import { Confirm, Notice } from "../../ui";

export type Pending = { row: OnlineEvalConfigRow; action: "pause" | "resume" | "delete" };

/** The 持续评估 tab of 评估任务 — where the former 在线评估 page lives now. */
export const onlineList = "/v3/tasks?tab=online";

/**
 * An online config page under 评估任务, with the former `/v3/online` vocabulary:
 * `view=new` → the continuous create form, `view=detail|edit` + `id` → that config's
 * page (`kind=online`); anything else → the 持续评估 tab.
 */
export function onlineHref(params: Record<string, string>): string {
  const { view, id } = params;
  if (view === "new") return `/v3/tasks?${new URLSearchParams({ view: "new", strategy: "continuous", ...(params.agent ? { agent: params.agent } : {}) })}`;
  if ((view === "detail" || view === "edit") && id) return `/v3/tasks?${new URLSearchParams({ view, kind: "online", id })}`;
  return onlineList;
}

/** An old `/v3/online` search → its 评估任务 URL (bookmarks, links from before the merge). */
export const v3OnlineToTasks = (search: string) => onlineHref(Object.fromEntries(new URLSearchParams(search)));

/** Pause / resume / delete with V2's confirm copy and guards; shared by the list and a config. */
export function useOnlineAction(onDone: (action: Pending["action"]) => void) {
  const { t } = useTranslation();
  const toast = useToast();
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  // shown in the still-open dialog with its AWS details, cleared with the dialog
  const [failure, setFailure] = useState<unknown>(null);
  const ask = (next: Pending | null) => {
    setFailure(null);
    setPending(next);
  };
  const run = async () => {
    if (!pending) return;
    const { row, action } = pending;
    setBusy(true);
    setFailure(null);
    try {
      if (action === "delete") await api.v2DeleteOnlineConfig(row.config_id);
      else await api.v2OnlineAction(row.config_id, action);
      toast("ok", action === "delete" ? t("v2.online.deleted", { logGroup: row.results_log_group }) : t(`v2.tasks.done.${action}`));
      setPending(null);
      onDone(action);
    } catch (err) {
      setFailure(err);
    } finally {
      setBusy(false);
    }
  };
  const dialog = pending ? (
    <Confirm
      title={t(`v2.online.confirm.${pending.action}Title`)}
      confirmLabel={t(`v2.online.confirm.${pending.action}Ok`)}
      cancelLabel={t("v3.common.cancel")}
      danger={pending.action === "delete"}
      busy={busy}
      onCancel={() => ask(null)}
      onConfirm={() => void run()}
    >
      {t(`v2.online.confirm.${pending.action}Body`, { name: configName(pending.row), logGroup: pending.row.results_log_group })}
      {failure != null && <div style={{ marginTop: 12 }}><Notice s="act"><ApiErrorDetail error={failure} /></Notice></div>}
    </Confirm>
  ) : null;
  return { ask, busy, dialog };
}
