import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, errorMessage, v2KnowledgeApi } from "../../../lib/api";
import { extractConflictAgents } from "../../../lib/knowledgeBases";
import { useV2Toast } from "../../hooks";
import { Confirm } from "../../ui";

/**
 * Delete a KB: confirm → DELETE; a 409 naming mounted agents opens a second
 * confirm that deletes with `force=true`. Other failures toast.
 */
export function useKbDelete(onDeleted: (kbId: string) => void) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [target, setTarget] = useState<{ kb_id: string; name: string } | null>(null);
  const [conflict, setConflict] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async (force: boolean) => {
    if (!target) return;
    setBusy(true);
    try {
      await v2KnowledgeApi.remove(target.kb_id, force);
      toast("success", t("knowledge.detail.deleted", { name: target.name }));
      setTarget(null);
      setConflict(null);
      onDeleted(target.kb_id);
    } catch (err) {
      if (err instanceof ApiError && err.code === "kb.has_attached_agents" && !force) {
        setConflict(extractConflictAgents({ detail: err.detail }));
      } else {
        toast("error", t("common.actionFailed", { msg: errorMessage(err) }));
        setTarget(null);
        setConflict(null);
      }
    } finally {
      setBusy(false);
    }
  };

  const close = () => {
    setTarget(null);
    setConflict(null);
  };

  const dialog = (
    <>
      <Confirm
        open={target !== null && conflict === null}
        title={t("knowledge.detail.delete.title")}
        body={t("knowledge.detail.delete.body", { name: target?.name ?? "" })}
        confirmLabel={t("v2.common.delete")}
        danger
        busy={busy}
        onConfirm={() => void run(false)}
        onClose={close}
      />
      <Confirm
        open={target !== null && conflict !== null}
        title={t("knowledge.detail.delete.conflictTitle")}
        body={t("knowledge.detail.delete.conflictBody", { agents: conflict?.join(", ") || "—" })}
        confirmLabel={t("knowledge.detail.delete.force")}
        danger
        busy={busy}
        onConfirm={() => void run(true)}
        onClose={close}
      />
    </>
  );
  return { ask: (kb: { kb_id: string; name: string }) => setTarget(kb), dialog };
}
