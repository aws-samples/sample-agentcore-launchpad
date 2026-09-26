import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { api, type AssistantConversationFootprint } from "../../../lib/api";
import { useV2Toast } from "../../hooks";
import { Alert, Confirm } from "../../ui";
import { useApiMessage } from "./common";

/**
 * CLEAR a conversation and everything it created: the footprint is read first and
 * shown in the confirm (exactly what the purge would remove and what blocks it);
 * the purge itself may take a while (fenced cleanups + teardown).
 */
export function useClearConversation(onCleared: (id: string) => void) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const apiMessage = useApiMessage();
  const { isAdmin } = useAuth();
  const [clearing, setClearing] = useState<{
    id: string;
    title: string;
    footprint: AssistantConversationFootprint | null;
    deleting: boolean;
  } | null>(null);

  const ask = async (c: { id: string; title: string }) => {
    const title = c.title || c.id.slice(0, 8);
    setClearing({ id: c.id, title, footprint: null, deleting: false });
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
    if (fp.blockers.length > 0) {
      toast("error", t("assistantPage.clear.blocked"));
      return;
    }
    if (fp.requires_admin && !isAdmin) {
      toast("error", t("assistantPage.clear.adminOnly"));
      return;
    }
    const { id, title } = clearing;
    setClearing({ ...clearing, deleting: true });
    try {
      const res = await api.assistantDeleteConversation(id);
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
    } catch (err) {
      toast("error", apiMessage(err));
    } finally {
      setClearing(null);
    }
  };

  const fp = clearing?.footprint ?? null;
  const cloud = fp ? fp.operations.filter((o) => o.status !== "cleaned") : [];
  const closing = fp
    ? fp.blockers.length
      ? { tone: "error" as const, text: t("assistantPage.clear.blockers", { list: fp.blockers.map((b) => b.reason).join("; ") }) }
      : fp.requires_admin && !isAdmin
        ? { tone: "warn" as const, text: t("assistantPage.clear.adminOnly") }
        : fp.agents.length || cloud.length
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
        if (!clearing?.deleting) setClearing(null);
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
            {!fp.agents.length && !cloud.length && !fp.datasets.length && (
              <div className="v2-muted">{t("assistantPage.clear.onlyTranscript")}</div>
            )}
            {closing && <Alert tone={closing.tone}>{closing.text}</Alert>}
          </div>
        )
      }
    />
  );

  return { ask, dialog, busy: clearing !== null };
}
