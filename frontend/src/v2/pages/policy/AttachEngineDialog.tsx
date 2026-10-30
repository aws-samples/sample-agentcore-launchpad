import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type GatewayPolicyEngine, type PolicyAttachmentMode, type PolicyEngine } from "../../../lib/api";
import { governanceError } from "../../../lib/governance";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Field, Modal, OptionCard, Select } from "../../ui";

const ATTACH_MODES: PolicyAttachmentMode[] = ["LOG_ONLY", "ENFORCE"];

/**
 * Attach / change a gateway's policy engine (Global `policyEngineConfiguration`: ONE engine per
 * gateway, a mode `LOG_ONLY` | `ENFORCE`). `PUT /api/gateways/{gid}/policy-engine` is
 * idempotent — changing the engine or the mode rewrites the bundle; the first attach also
 * attaches the interceptor to the gateway. `engineId` pins the engine (the engine detail's
 * 附加到网关 tab); without it the dialog offers every engine (the Gateway detail).
 */
export function AttachEngineDialog({
  open,
  gatewayId,
  gatewayName,
  current,
  engineId = null,
  engines = null,
  onClose,
  onDone,
}: {
  open: boolean;
  gatewayId: string;
  gatewayName: string;
  /** the gateway's attachment today (null = unknown / none) */
  current: GatewayPolicyEngine | null;
  engineId?: string | null;
  /** the engines to choose from when `engineId` is not pinned */
  engines?: PolicyEngine[] | null;
  onClose: () => void;
  onDone: (next: GatewayPolicyEngine) => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [engine, setEngine] = useState(engineId ?? current?.engine_id ?? "");
  const [mode, setMode] = useState<PolicyAttachmentMode>(current?.mode ?? "LOG_ONLY");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!open) return;
    setEngine(engineId ?? current?.engine_id ?? "");
    setMode(current?.mode ?? "LOG_ONLY");
  }, [open, engineId, current]);

  const attached = current?.attached === true;
  const changingEngine = attached && !!engine && engine !== current?.engine_id;
  const ready = !!engine && !busy && (!attached || engine !== current?.engine_id || mode !== current?.mode);

  const submit = async () => {
    setBusy(true);
    try {
      const next = await api.attachPolicyEngine(gatewayId, { engine_id: engine, mode });
      toast("success", t(changingEngine ? "v2.policy.attach.replaced" : "v2.policy.attach.saved", { gateway: gatewayName }));
      onDone(next);
    } catch (err) {
      toast("error", governanceError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      title={t(attached ? "v2.policy.attach.changeTitle" : "v2.policy.attach.title", { gateway: gatewayName })}
      onClose={onClose}
      testId="v2-policy-attach-dialog"
      footer={
        <>
          <Button onClick={onClose} testId="v2-policy-attach-cancel">{t("v2.common.cancel")}</Button>
          <Button kind={mode === "ENFORCE" ? "danger" : "primary"} disabled={!ready} onClick={() => void submit()} testId="v2-policy-attach-ok">
            {t(attached ? "v2.policy.attach.change" : "v2.policy.attach.attach")}
          </Button>
        </>
      }
    >
      <div className="v2-stack">
        {engineId === null && (
          <Field label={t("v2.policy.engine")} required>
            <Select
              mono
              value={engine}
              disabled={busy}
              placeholder={t("v2.policy.attach.pickEngine")}
              options={(engines ?? []).map((e) => ({ value: e.id, label: e.name }))}
              onChange={setEngine}
              testId="v2-policy-attach-engine"
            />
          </Field>
        )}
        {changingEngine && <Alert tone="warn">{t("v2.policy.attach.replaceNote", { from: current?.engine_name ?? current?.engine_id ?? "" })}</Alert>}
        <Field label={t("v2.policy.attach.mode")} full hint={t(`v2.policy.attach.modeHelp.${mode.toLowerCase()}`)}>
          <div className="v2-options">
            {ATTACH_MODES.map((option) => (
              <OptionCard
                key={option}
                title={t(`v2.policy.attachMode.${option.toLowerCase()}`)}
                desc={t(`v2.policy.attach.modeDesc.${option.toLowerCase()}`)}
                on={mode === option}
                disabled={busy}
                onClick={() => setMode(option)}
                testId={`v2-policy-attach-mode-${option.toLowerCase()}`}
              />
            ))}
          </div>
        </Field>
        {mode === "ENFORCE" && <Alert tone="warn">{t("v2.policy.attach.enforceNote")}</Alert>}
      </div>
    </Modal>
  );
}
