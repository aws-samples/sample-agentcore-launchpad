import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  clearCanaryList,
  clearExperimentList,
  clearInProgress,
  clearStepText,
  runConversationClear,
} from "../../../lib/assistant";
import { api, type AssistantConversationFootprint } from "../../../lib/api";
import { useV2Toast } from "../../hooks";
import { Alert, Confirm } from "../../ui";
import { useApiMessage } from "./common";

/**
 * CLEAR a conversation and everything it created: the footprint is read first and
 * shown in the confirm (exactly what the purge would remove — including the canaries
 * and experiments on its Agents — and what blocks it). A cloud clear is a durable
 * server-side job: the dialog follows its steps, and closing it only stops following.
 */
export function useClearConversation(onCleared: (id: string) => void) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const apiMessage = useApiMessage();
  const [clearing, setClearing] = useState<{
    id: string;
    title: string;
    footprint: AssistantConversationFootprint | null;
    deleting: boolean;
    progress: string;
  } | null>(null);
  const detached = useRef(false);

  const ask = async (c: { id: string; title: string }) => {
    const title = c.title || c.id.slice(0, 8);
    setClearing({ id: c.id, title, footprint: null, deleting: false, progress: "" });
    try {
      const footprint = await api.assistantConversationFootprint(c.id);
      setClearing((cur) => (cur?.id === c.id ? { ...cur, footprint } : cur));
    } catch (err) {
      setClearing(null);
      toast("error", apiMessage(err));
    }
  };

  const run = async () => {
    const fp = clearing?.footprint;
    if (!clearing || !fp || clearing.deleting) return;
    const following = clearInProgress(fp);
    if (!following && fp.blockers.length > 0) {
      toast("error", t("assistantPage.clear.blocked"));
      return;
    }
    if (!following && !fp.can_clear) {
      toast("error", t("assistantPage.clear.noPermission"));
      return;
    }
    const { id, title } = clearing;
    detached.current = false;
    setClearing({ ...clearing, deleting: true, progress: t("assistantPage.clear.starting") });
    try {
      const outcome = await runConversationClear(id, {
        detached: () => detached.current,
        onProgress: (text) =>
          setClearing((cur) => (cur?.id === id ? { ...cur, progress: text } : cur)),
      });
      if (outcome.kind === "done") {
        const res = outcome.result;
        toast(
          "success",
          t("assistantPage.clear.doneToast", {
            title,
            agents: res.agents.length,
            operations: res.operations_cleaned.length,
            datasets: res.datasets.length,
          }),
        );
        onCleared(id);
      } else if (outcome.kind === "failed") {
        toast("error", t("assistantPage.clear.failedToast", { reason: outcome.reason }));
      }
    } catch (err) {
      toast("error", apiMessage(err));
    } finally {
      if (!detached.current) setClearing(null);
    }
  };

  const fp = clearing?.footprint ?? null;
  const cloud = fp ? fp.operations.filter((o) => o.status !== "cleaned") : [];
  const experiments = fp ? clearExperimentList(fp) : "";
  const following = fp ? clearInProgress(fp) : false;
  const closing = fp
    ? following
      ? { tone: "info" as const, text: t("assistantPage.clear.inProgress", { step: clearStepText(fp.purge?.step) || fp.purge?.status }) }
      : fp.blockers.length
        ? { tone: "error" as const, text: t("assistantPage.clear.blockers", { list: fp.blockers.map((b) => b.reason).join("; ") }) }
        : !fp.can_clear
          ? { tone: "warn" as const, text: t("assistantPage.clear.noPermission") }
          : fp.agents.length || cloud.length || fp.canaries.length
            ? { tone: "warn" as const, text: t("assistantPage.clear.irreversible") }
            : { tone: "info" as const, text: t("assistantPage.clear.irreversibleLocal") }
    : null;

  const dialog = (
    <Confirm
      open={fp !== null}
      title={t("assistantPage.clear.title", { title: clearing?.title ?? "" })}
      danger
      busy={clearing?.deleting}
      confirmLabel={clearing?.deleting ? t("assistantPage.clear.working") : t("assistantPage.clear.confirm")}
      onConfirm={() => void run()}
      onClose={() => {
        if (clearing?.deleting) {
          // the job keeps running server-side; only the polling stops
          detached.current = true;
          toast("success", t("assistantPage.clear.background"));
        }
        setClearing(null);
      }}
      body={
        fp && (
          <div className="v2-stack" data-testid="v2-assistant-clear-body">
            <div>{t("assistantPage.clear.intro", { turns: fp.turns, proposals: fp.proposals })}</div>
            {fp.agents.length > 0 && (
              <div>
                {t("assistantPage.clear.agents", {
                  list: fp.agents.map((a) => `${a.name} (${a.status})`).join(", "),
                })}
              </div>
            )}
            {fp.canaries.length > 0 && (
              <div data-testid="v2-assistant-clear-canaries">
                {t("assistantPage.clear.canaries", { list: clearCanaryList(fp) })}
              </div>
            )}
            {experiments && <div>{t("assistantPage.clear.experiments", { list: experiments })}</div>}
            {(fp.canaries.length > 0 || fp.experiments.length > 0) && (
              <div className="v2-muted">{t("assistantPage.clear.historyKept")}</div>
            )}
            {cloud.length > 0 && (
              <div>
                {t("assistantPage.clear.operations", {
                  n: cloud.length,
                  resources: cloud.reduce((acc, o) => acc + o.cloud_resources, 0),
                })}
              </div>
            )}
            {fp.datasets.length > 0 && (
              <div>
                {t("assistantPage.clear.datasets", {
                  list: fp.datasets.map((d) => `${d.name} (${t("assistantEval.items", { n: d.item_count })})`).join(", "),
                })}
                {fp.datasets.some((d) => d.cloud) && <div className="v2-muted">{t("assistantPage.clear.datasetCloud")}</div>}
              </div>
            )}
            {!fp.agents.length && !cloud.length && !fp.datasets.length && !fp.canaries.length && (
              <div className="v2-muted">{t("assistantPage.clear.onlyTranscript")}</div>
            )}
            {fp.purge?.status === "failed" && !clearing?.deleting && (
              <Alert tone="warn">
                {t("assistantPage.clear.lastFailed", { reason: fp.purge.blocker?.message || fp.purge.error || "" })}
              </Alert>
            )}
            {clearing?.deleting ? (
              <Alert tone="info">
                <span data-testid="v2-assistant-clear-progress">
                  {t("assistantPage.clear.progress", { step: clearing.progress })}
                </span>
              </Alert>
            ) : (
              closing && <Alert tone={closing.tone}>{closing.text}</Alert>
            )}
          </div>
        )
      }
    />
  );

  return { ask, dialog, busy: clearing !== null };
}
