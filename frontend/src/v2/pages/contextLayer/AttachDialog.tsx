import { Bot, Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { methodLabel } from "../../../components/methodChipMeta";
import { api } from "../../../lib/api";
import { useLoad } from "../../hooks";
import { Alert, Button, Field, Modal, Select, Spin } from "../../ui";
import { attachedNamespace, attachTargets, editAgentLink, newAgentLink } from "./attach";

/**
 * "挂到 Agent / Attach to agent": a new agent opens the wizard with this namespace
 * prefilled (Harness by default, or Strands); an existing Harness or HTTP Strands
 * agent the member may redeploy opens its editor with the namespace prefilled.
 * Nothing is saved here — the wizard's create / re-publish does that.
 */
export function AttachDialog({ ns, onClose }: { ns: { id: string; name: string }; onClose: () => void }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { can } = useAuth();
  const canDeploy = can("agents.deploy");
  const agents = useLoad(() => api.listAgents(), "context-layer-attach-agents");
  const targets = useMemo(() => attachTargets(agents.data?.agents ?? [], canDeploy), [agents.data, canDeploy]);
  const [pick, setPick] = useState("");
  const chosen = targets.find((a) => a.id === pick) ?? null;
  const current = chosen ? attachedNamespace(chosen) : null;

  return (
    <Modal
      open
      title={t("v2.contextLayer.attach.title", { name: ns.name })}
      onClose={onClose}
      testId="context-layer-attach"
      footer={<Button onClick={onClose}>{t("v2.common.cancel")}</Button>}
    >
      {!canDeploy && <Alert tone="warn">{t("v2.contextLayer.attach.noPermission")}</Alert>}
      <h3 className="v2-sub-title">{t("v2.contextLayer.attach.newTitle")}</h3>
      <p className="v2-muted">{t("v2.contextLayer.attach.newHint")}</p>
      <div className="v2-row">
        <Button kind="primary" disabled={!canDeploy} onClick={() => navigate(newAgentLink(ns, "harness"))} testId="context-layer-attach-new-harness">
          <Plus size={14} aria-hidden="true" />
          {t("v2.contextLayer.attach.newHarness")}
        </Button>
        <Button disabled={!canDeploy} onClick={() => navigate(newAgentLink(ns, "zip_runtime"))} testId="context-layer-attach-new-strands">
          <Plus size={14} aria-hidden="true" />
          {t("v2.contextLayer.attach.newStrands")}
        </Button>
      </div>
      <h3 className="v2-sub-title">{t("v2.contextLayer.attach.existingTitle")}</h3>
      {agents.loading && !agents.data ? (
        <Spin />
      ) : agents.error ? (
        <Alert tone="error">{agents.error}</Alert>
      ) : targets.length === 0 ? (
        <p className="v2-muted" data-testid="context-layer-attach-none">
          {t("v2.contextLayer.attach.none")}
        </p>
      ) : (
        <div className="v2-form">
          <Field label={t("v2.contextLayer.attach.agent")} hint={t("v2.contextLayer.attach.existingHint")}>
            <Select
              value={pick}
              placeholder={t("v2.contextLayer.attach.pick")}
              options={targets.map((a) => ({ value: a.id, label: `${a.name} · ${methodLabel(a.method)}` }))}
              onChange={setPick}
              testId="context-layer-attach-agent"
            />
          </Field>
          {current && current.id !== ns.id && (
            <Alert tone="warn">{t("v2.contextLayer.attach.replaces", { name: current.name || current.id })}</Alert>
          )}
          {current && current.id === ns.id && <Alert>{t("v2.contextLayer.attach.already")}</Alert>}
          <div className="v2-row">
            <Button disabled={!chosen} onClick={() => chosen && navigate(editAgentLink(chosen.id, ns))} testId="context-layer-attach-edit">
              <Bot size={14} aria-hidden="true" />
              {t("v2.contextLayer.attach.openEditor")}
            </Button>
          </div>
        </div>
      )}
    </Modal>
  );
}
