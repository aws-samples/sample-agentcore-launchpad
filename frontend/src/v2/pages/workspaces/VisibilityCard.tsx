import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, errorMessage, type ResourceVisibility } from "../../../lib/api";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Field, Segmented, Spin } from "../../ui";
import { VisibilityTag } from "./tags";

/**
 * The workspace's member-visibility mode. `shared` keeps today's behavior;
 * `isolated` lets each member see only what they created (plus admin-created
 * and creator-unknown resources). Saving moves no data — the next request of
 * every member is filtered by the new mode. The page is admin-only, and so is
 * the route behind this card.
 */
export function VisibilityCard({ workspaceId, onSaved }: { workspaceId: string; onSaved?: () => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const loaded = useLoad(() => api.getWorkspaceVisibility(workspaceId), `ws-visibility:${workspaceId}`);
  const [mode, setMode] = useState<ResourceVisibility>("shared");
  const [saving, setSaving] = useState(false);
  const saved = loaded.data?.resource_visibility;

  // Back to the saved mode: on load, after a save, and on discard.
  const reset = useCallback(() => {
    if (saved) setMode(saved);
  }, [saved]);
  useEffect(reset, [reset]);
  const changed = !!saved && mode !== saved;

  const save = async () => {
    setSaving(true);
    try {
      await api.putWorkspaceVisibility(workspaceId, mode);
      toast("success", t("v2.workspaces.visibility.saved"));
      loaded.reload();
      onSaved?.();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card
      title={t("v2.workspaces.visibility.title")}
      sub={t("v2.workspaces.visibility.sub")}
      testId="v2-ws-visibility"
      end={
        saved ? (
          <span className="v2-row">
            <Button disabled={saving || !changed} onClick={reset} testId="v2-ws-visibility-discard">
              {t("v2.workspaces.visibility.discard")}
            </Button>
            <Button
              kind="primary"
              disabled={saving || !changed}
              onClick={() => void save()}
              testId="v2-ws-visibility-save"
            >
              {t("v2.common.save")}
            </Button>
          </span>
        ) : undefined
      }
    >
      {loaded.error && <Alert tone="error">{loaded.error}</Alert>}
      {loaded.loading && !saved && <Spin />}
      {saved && (
        <>
          <p className="v2-muted">
            {t("v2.workspaces.visibility.current")} <VisibilityTag mode={saved} />
          </p>
          <div className="v2-form">
            <Field label={t("v2.workspaces.visibility.label")} full>
              <Segmented<ResourceVisibility>
                value={mode}
                ariaLabel={t("v2.workspaces.visibility.label")}
                options={[
                  { value: "shared", label: t("v2.workspaces.visibility.shared") },
                  { value: "isolated", label: t("v2.workspaces.visibility.isolated") },
                ]}
                onChange={setMode}
              />
            </Field>
          </div>
          <p className="v2-muted" data-testid="v2-ws-visibility-hint">
            {t(mode === "isolated" ? "v2.workspaces.visibility.isolatedHint" : "v2.workspaces.visibility.sharedHint")}
          </p>
        </>
      )}
    </Card>
  );
}
