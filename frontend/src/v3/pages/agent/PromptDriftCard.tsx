import { Download } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type AgentPromptDrift, errorMessage } from "../../../lib/api";
import { ago } from "../../format";
import { type Loaded, useLoad, useToast } from "../../hooks";
import { Btn, Chip, Confirm, Notice, Panel, Skeleton } from "../../ui";

/**
 * Prompt drift: the prompt this page shows (the ledger's) beside the one the Harness's
 * latest AWS version carries. An edit in the AWS console shows up here; adopting it
 * re-publishes the agent with the AWS prompt, so the page, the snapshots and the next
 * deploy carry it (the redeploy's permission and Agent-DLC gate apply).
 */
export function PromptDriftView({
  drift,
  canAdopt,
  blockedTitle,
  onAdopted,
}: {
  drift: Loaded<AgentPromptDrift>;
  canAdopt: boolean;
  blockedTitle?: string;
  onAdopted: () => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const d = drift.data;
  const adopt = async () => {
    if (!d?.aws_version) return;
    setBusy(true);
    setError(null);
    try {
      await api.adoptAgentPrompt(d.agent_id, d.aws_version);
      toast("ok", t("v3.agent.promptDrift.adopted", { version: d.aws_version }));
      setConfirm(false);
      onAdopted();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel title={t("v3.agent.promptDrift.title")}>
      <div style={{ display: "grid", gap: 10 }} data-testid="v3-prompt-drift">
        {drift.error ? (
          <Notice s="act">
            {drift.error}
            <button type="button" className="v3-btn sm" onClick={drift.reload}>{t("v3.ux.retry")}</button>
          </Notice>
        ) : !d ? (
          <Skeleton rows={2} />
        ) : d.status === "unsupported" ? (
          <span className="v3-hint">{t(`v3.agent.promptDrift.unsupported.${d.reason ?? "runtime"}`)}</span>
        ) : d.status === "in_sync" ? (
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Chip s="ok">{t("v3.agent.promptDrift.inSync")}</Chip>
            <span className="v3-hint">{t("v3.agent.promptDrift.inSyncHint", { version: d.aws_version ?? "?" })}</span>
          </div>
        ) : (
          <>
            <Notice s="wait">
              {t("v3.agent.promptDrift.drifted", { version: d.aws_version ?? "?", ledger: d.ledger_version ?? "?" })}
              {d.aws_updated_at ? ` (${ago(d.aws_updated_at)})` : ""}
              {d.endpoint_mode === "live" ? ` ${t("v3.agent.promptDrift.liveNote")}` : ""}
            </Notice>
            <div className="pair">
              <div style={{ display: "grid", gap: 4 }}>
                <b>{t("v3.agent.promptDrift.ledger")}</b>
                <pre className="v3-pre" data-testid="v3-prompt-drift-ledger">{d.ledger_prompt}</pre>
              </div>
              <div style={{ display: "grid", gap: 4 }}>
                <b>{t("v3.agent.promptDrift.aws", { version: d.aws_version ?? "?" })}</b>
                <pre className="v3-pre" data-testid="v3-prompt-drift-aws">{d.aws_prompt}</pre>
              </div>
            </div>
            {(d.other_differences ?? []).length > 0 && (
              <Notice s="info">
                {t("v3.agent.promptDrift.others", {
                  members: (d.other_differences ?? []).map((o) => `${o.member} (${String(o.ledger)} → ${String(o.aws)})`).join(", "),
                })}
              </Notice>
            )}
            <div>
              <Btn kind="primary" size="sm" disabled={!canAdopt} disabledReason={canAdopt ? undefined : blockedTitle}
                onClick={() => setConfirm(true)}>
                <Download size={13} /> {t("v3.agent.promptDrift.adopt")}
              </Btn>
            </div>
          </>
        )}
      </div>
      {confirm && d && (
        <Confirm
          title={t("v3.agent.promptDrift.confirmTitle")}
          confirmLabel={t("v3.agent.promptDrift.adopt")}
          cancelLabel={t("v3.common.cancel")}
          busy={busy}
          onConfirm={() => void adopt()}
          onCancel={() => !busy && setConfirm(false)}
        >
          <p style={{ margin: 0 }}>{t("v3.agent.promptDrift.confirmBody", { version: d.aws_version ?? "?" })}</p>
          {error && <Notice s="act">{error}</Notice>}
        </Confirm>
      )}
    </Panel>
  );
}

/** Loads the drift for one Harness agent and renders `PromptDriftView`. */
export function PromptDriftCard({
  agentId,
  refreshKey,
  canAdopt,
  blockedTitle,
  onAdopted,
}: {
  agentId: string;
  refreshKey: string;
  canAdopt: boolean;
  blockedTitle?: string;
  onAdopted: () => void;
}) {
  const drift = useLoad(() => api.agentPromptDrift(agentId), `prompt-drift:${agentId}:${refreshKey}`);
  return <PromptDriftView drift={drift} canAdopt={canAdopt} blockedTitle={blockedTitle} onAdopted={onAdopted} />;
}
