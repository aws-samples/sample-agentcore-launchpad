import { useState } from "react";
import { useTranslation } from "react-i18next";

import { agentProjectSettingsSource, needsConfirm } from "../../../lib/agent-project-settings";
import { api, errorMessage } from "../../../lib/api";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, Field, Segmented, Spin } from "../../ui";

type Choice = "on" | "off";

/**
 * The agent-project switch (`/api/settings/agent-projects`, admin-only, hub-global):
 * may a new agent be filed under a workspace `default` project? Off by default. The
 * card states the impact next to the switch — such agents belong to no department —
 * and turning it on asks for a confirmation. The page is admin-only, and so is the
 * route.
 */
export function AgentProjectSettingsCard() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const loaded = useLoad(() => api.agentProjectSettings(), "agent-project-settings");
  const [saving, setSaving] = useState(false);
  const [asking, setAsking] = useState(false);
  const view = loaded.data;
  const current: Choice = view?.allow_default_project ? "on" : "off";
  const base = "v3.create.project.settings";

  const save = async (value: boolean | null) => {
    setSaving(true);
    try {
      await api.setAgentProjectSettings(value);
      toast("success", t(`${base}.saved`));
      loaded.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setSaving(false);
      setAsking(false);
    }
  };

  const choose = (next: Choice) => {
    if (!view || next === current) return;
    if (needsConfirm(view.allow_default_project, next === "on")) setAsking(true);
    else void save(false);
  };

  return (
    <Card
      title={t(`${base}.title`)}
      sub={t(`${base}.sub`)}
      testId="v2-agent-project-settings"
      end={
        view?.source === "console" ? (
          <Button disabled={saving} onClick={() => void save(null)} testId="v2-agent-project-settings-clear">
            {t(`${base}.clear`)}
          </Button>
        ) : undefined
      }
    >
      {loaded.error && <Alert tone="error">{loaded.error}</Alert>}
      {loaded.loading && !view && <Spin />}
      {view && (
        <>
          <div className="v2-form">
            <Field label={t(`${base}.label`)} full>
              <Segmented<Choice>
                value={current}
                ariaLabel={t(`${base}.label`)}
                options={[
                  { value: "off", label: t(`${base}.off`) },
                  { value: "on", label: t(`${base}.on`) },
                ]}
                onChange={choose}
              />
            </Field>
          </div>
          {view.allow_default_project ? (
            <Alert tone="warn">
              <span data-testid="v2-agent-project-settings-impact">{t(`${base}.impactOn`)}</span>
            </Alert>
          ) : (
            <p className="v2-muted" data-testid="v2-agent-project-settings-impact">
              {t(`${base}.impactOff`)}
            </p>
          )}
          <p className="v2-muted" data-testid="v2-agent-project-settings-source">
            {agentProjectSettingsSource(view, t)}
          </p>
        </>
      )}
      <Confirm
        open={asking}
        title={t(`${base}.title`)}
        body={t(`${base}.confirmOn`)}
        confirmLabel={t(`${base}.on`)}
        busy={saving}
        onConfirm={() => void save(true)}
        onClose={() => setAsking(false)}
      />
    </Card>
  );
}
