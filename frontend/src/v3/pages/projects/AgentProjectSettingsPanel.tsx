import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { ErrorCallout } from "../../../components/ErrorCallout";
import { agentProjectSettingsSource, needsConfirm } from "../../../lib/agent-project-settings";
import { api } from "../../../lib/api";
import { useFailure } from "../../failure";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Confirm, Notice, Panel, Skeleton } from "../../ui";

/**
 * The agent-project switch on the projects page, administrators only
 * (`/api/settings/agent-projects`): may a new agent be filed under a workspace
 * `default` project? The impact sits next to the switch, and turning it on asks
 * first — such agents belong to no department.
 */
export function AgentProjectSettingsPanel() {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const loaded = useLoad(() => api.agentProjectSettings(), "v3-agent-project-settings");
  const [busy, setBusy] = useState(false);
  const [asking, setAsking] = useState(false);
  const view = loaded.data;
  const base = "v3.create.project.settings";

  const save = async (value: boolean | null) => {
    setBusy(true);
    try {
      failed.clear();
      await api.setAgentProjectSettings(value);
      toast("ok", t(`${base}.saved`));
      loaded.reload();
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(false);
      setAsking(false);
    }
  };

  const flip = () => {
    if (!view) return;
    const next = !view.allow_default_project;
    if (needsConfirm(view.allow_default_project, next)) setAsking(true);
    else void save(false);
  };

  return (
    <Panel
      title={t(`${base}.title`)}
      end={
        view ? (
          <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Chip s={view.allow_default_project ? "wait" : undefined}>
              {t(view.allow_default_project ? `${base}.on` : `${base}.off`)}
            </Chip>
            <Btn size="sm" kind={view.allow_default_project ? undefined : "primary"} busy={busy} onClick={flip}>
              {t(view.allow_default_project ? `${base}.off` : `${base}.on`)}
            </Btn>
            {view.source === "console" && (
              <Btn size="sm" kind="ghost" disabled={busy} onClick={() => void save(null)}>
                {t(`${base}.clear`)}
              </Btn>
            )}
          </span>
        ) : undefined
      }
    >
      <div style={{ display: "grid", gap: 8 }} data-testid="v3-agent-project-settings">
        <small className="v3-hint">{t(`${base}.sub`)}</small>
        <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-agent-project-settings-error" />
        {loaded.error ? (
          <Notice s="act">
            <ApiErrorDetail error={loaded.failure ?? loaded.error} />
          </Notice>
        ) : !view ? (
          <Skeleton rows={1} />
        ) : (
          <>
            {view.allow_default_project ? (
              <Notice s="wait">{t(`${base}.impactOn`)}</Notice>
            ) : (
              <small className="v3-hint">{t(`${base}.impactOff`)}</small>
            )}
            <small className="v3-hint">{agentProjectSettingsSource(view, t)}</small>
          </>
        )}
      </div>
      {asking && (
        <Confirm
          title={t(`${base}.title`)}
          confirmLabel={t(`${base}.on`)}
          cancelLabel={t("v3.common.cancel")}
          busy={busy}
          onConfirm={() => void save(true)}
          onCancel={() => setAsking(false)}
        >
          {t(`${base}.confirmOn`)}
        </Confirm>
      )}
    </Panel>
  );
}
