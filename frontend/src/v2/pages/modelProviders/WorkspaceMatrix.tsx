import { useTranslation } from "react-i18next";

import type { ModelProviderOut, ModelRowOut, ModelWorkspaceFlags } from "../../../lib/api";
import { Alert, Card, Tag } from "../../ui";
import type { useModelProviderActions } from "./actions";

type Actions = ReturnType<typeof useModelProviderActions>;

/**
 * `?view=workspaces` (plan §2.4 / §8): workspace × model matrix of per-workspace enable
 * flags. A checked cell = members of that workspace may pick the model (absent row =
 * enabled); unchecking writes the workspace's `disabled_models` list.
 */
export function WorkspaceMatrix({
  providers,
  models,
  workspaces,
  actions,
}: {
  providers: ModelProviderOut[];
  models: ModelRowOut[];
  workspaces: ModelWorkspaceFlags[];
  actions: Actions;
}) {
  const { t } = useTranslation();
  const rows = [...models].sort((a, b) => a.sort_order - b.sort_order);
  const providerLabel = (m: ModelRowOut) => providers.find((p) => p.id === m.provider_id)?.label ?? m.provider_id;
  const toggle = (ws: ModelWorkspaceFlags, m: ModelRowOut, enabled: boolean) => {
    const next = enabled ? ws.disabled_models.filter((id) => id !== m.model_id) : [...new Set([...ws.disabled_models, m.model_id])];
    void actions.setWorkspaceFlags(ws.id, next);
  };
  return (
    <Card title={t("modelProviders.workspaces.title")} sub={t("modelProviders.workspaces.sub")} testId="v2-mp-workspaces">
      <Alert tone="info">{t("modelProviders.workspaces.hint")}</Alert>
      <div className="v2-table-wrap" style={{ marginTop: 12 }}>
        <table className="v2-table dense v2-mp-matrix" data-testid="v2-mp-matrix">
          <thead>
            <tr>
              <th>{t("modelProviders.col.model")}</th>
              {workspaces.map((ws) => (
                <th key={ws.id} className="ws" title={ws.id}>
                  {ws.name || ws.id}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={workspaces.length + 1} className="v2-muted">
                  {t("modelProviders.empty.models")}
                </td>
              </tr>
            )}
            {rows.map((m) => (
              <tr key={m.id}>
                <td>
                  <span className="v2-mp-model">
                    <b>{m.label}</b>
                    <span className="id">{m.model_id}</span>
                  </span>{" "}
                  <Tag tone="outline">{providerLabel(m)}</Tag>
                  {!m.enabled && <Tag tone="gray">{t("modelProviders.globallyDisabled")}</Tag>}
                </td>
                {workspaces.map((ws) => {
                  const enabled = !ws.disabled_models.includes(m.model_id);
                  return (
                    <td key={ws.id} className="cell">
                      <input
                        type="checkbox"
                        checked={enabled}
                        disabled={actions.busyId === `ws:${ws.id}`}
                        aria-label={`${ws.name || ws.id} · ${m.label}`}
                        onChange={(e) => toggle(ws, m, e.target.checked)}
                        data-testid={`v2-mp-flag-${ws.id}-${m.model_id}`}
                      />
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
