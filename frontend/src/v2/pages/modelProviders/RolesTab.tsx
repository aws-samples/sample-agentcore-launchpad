import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, api, errorMessage, type ModelProviderOut, type ModelRoleSetting, type ModelRowOut } from "../../../lib/api";
import { reloadModelDefaults, type RoleCatalogRow, roleModelOptions } from "../../../lib/model-defaults";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, type Column, Select, Spin, Table, Tag } from "../../ui";
import { roleHidden } from "./common";

/**
 * Settings → Model Providers → *Model roles* (`?view=roles`, r15-e — upstream #275 subset):
 * which catalog model the platform itself uses for each job (agent default, evaluation
 * judge, persona actor, prompt optimizer, Skill Lab judge / target). One row per role with
 * its effective model, its default, who overrode it, a picker over the registry's enabled
 * chat rows (tool-capable only where the role runs tools), Probe (the registry's own
 * re-probe of the picked row), Save and Reset. Writes are `PUT` / `DELETE
 * /api/settings/model-roles/{role}`; a refused pick surfaces the server's 422 reason.
 */
export function RolesTab({
  providers,
  models,
  onProbe,
  readOnly = false,
}: {
  providers: ModelProviderOut[];
  models: ModelRowOut[];
  onProbe: (provider: ModelProviderOut, model: ModelRowOut) => void;
  /** N17-obs251: a member sees the roles — effective model, default, "overridden by an
   *  administrator" — with no Save / Reset / Probe and the pickers disabled */
  readOnly?: boolean;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const defaults = useLoad(() => api.modelDefaults(), "model-defaults");
  const [picks, setPicks] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);

  // N17-obs252: a role of a hidden console feature (Skill Lab via `HIDDEN_SURFACES.features`,
  // the Studio coding agent via `HIDDEN_SURFACES.createEntries.studio`) is hidden with it — the
  // server keeps it
  const roles = useMemo(
    () => (defaults.data?.roles ?? []).filter((r) => !roleHidden(r.feature)),
    [defaults.data],
  );
  // every (re)load re-seeds the pickers from the effective values
  useEffect(() => {
    if (defaults.data) setPicks(Object.fromEntries(defaults.data.roles.map((r) => [r.role, r.model_id])));
  }, [defaults.data]);

  const providerById = useMemo(() => new Map(providers.map((p) => [p.id, p])), [providers]);
  const rowByModelId = useMemo(() => new Map(models.map((m) => [m.model_id, m])), [models]);
  // N17-obs203 §6: a platform role runs for every member — only models of WORKSPACE-scope
  // (shared) providers are offered; a personal provider's rows are not (the server refuses
  // them with 422 models.model_not_allowed reason personal_scope)
  const catalog = useMemo<RoleCatalogRow[]>(
    () =>
      models
        .filter((m) => providerById.get(m.provider_id)?.scope !== "personal")
        .map((m) => ({
          model_id: m.model_id,
          label: m.label,
          kind: m.kind,
          enabled: m.enabled,
          tools: m.tools,
          provider_enabled: providerById.get(m.provider_id)?.enabled !== false,
        })),
    [models, providerById],
  );

  const labelOf = (id: string) => {
    if (!id) return t("modelProviders.roles.noneOption");
    const row = rowByModelId.get(id);
    return row ? `${row.label} · ${id}` : id;
  };

  const changed = async (role: ModelRoleSetting, fn: () => Promise<ModelRoleSetting>, key: string) => {
    setBusy(role.role);
    try {
      const saved = await fn();
      toast("success", t(key, { role: t(`modelProviders.roles.role.${role.role}.label`, role.role), model: saved.model_id || t("modelProviders.roles.none") }));
      defaults.reload();
      void reloadModelDefaults();
    } catch (err) {
      // N17-obs252: the embedding role is the registry's active row — while a knowledge-base
      // or memory index holds documents the server refuses the change (409
      // embedding.reindex_required, `services/embedding_guard.py`); say what to do, not a code
      if (err instanceof ApiError && err.code === "embedding.reindex_required") {
        toast("error", t("modelProviders.roles.embeddingReindex"));
      } else {
        toast("error", t("modelProviders.actionFailed", { msg: errorMessage(err) }));
      }
    } finally {
      setBusy(null);
    }
  };

  const allColumns: Column<ModelRoleSetting>[] = [
    {
      key: "role",
      title: t("modelProviders.roles.col.role"),
      render: (r) => (
        <div className="v2-mp-role" data-testid={`v2-mp-role-${r.role}`}>
          <b>{t(`modelProviders.roles.role.${r.role}.label`, r.role)}</b>
          <span className="v2-muted">{t(`modelProviders.roles.role.${r.role}.hint`, "")}</span>
          <span className="v2-row">
            <code className="mono">{r.field}</code>
            {r.needs_tools && <Tag tone="blue">{t("modelProviders.roles.needsTools")}</Tag>}
          </span>
        </div>
      ),
    },
    {
      key: "model",
      title: t("modelProviders.roles.col.model"),
      render: (r) => {
        const options = roleModelOptions(r, catalog).map((id) => ({ value: id, label: labelOf(id) }));
        return (
          <Select
            value={picks[r.role] ?? r.model_id}
            options={options}
            placeholder={r.allow_empty ? t("modelProviders.roles.noneOption") : undefined}
            mono
            onChange={(v) => setPicks((prev) => ({ ...prev, [r.role]: v }))}
            disabled={readOnly || busy !== null}
            ariaLabel={t(`modelProviders.roles.role.${r.role}.label`, r.role)}
            testId={`v2-mp-role-select-${r.role}`}
          />
        );
      },
    },
    {
      key: "status",
      title: t("modelProviders.roles.col.status"),
      render: (r) => (
        <div className="v2-mp-role-status" data-testid={`v2-mp-role-status-${r.role}`}>
          {r.overridden_by !== null ? (
            // N17-obs251: the server redacts the administrator's name for members (`""`)
            <Tag tone="orange">{r.overridden_by ? t("modelProviders.roles.overriddenBy", { who: r.overridden_by }) : t("modelProviders.roles.overriddenByAdmin")}</Tag>
          ) : (
            <Tag tone="gray">{t("modelProviders.roles.builtin")}</Tag>
          )}
          <span className="v2-muted mono">{t("modelProviders.roles.default", { model: r.default || t("modelProviders.roles.none") })}</span>
        </div>
      ),
    },
    {
      key: "actions",
      title: t("modelProviders.roles.col.actions"),
      className: "v2-mp-actions",
      render: (r) => {
        const pick = picks[r.role] ?? r.model_id;
        const row = rowByModelId.get(pick);
        const provider = row ? providerById.get(row.provider_id) : undefined;
        const dirty = pick !== r.model_id;
        return (
          <span className="v2-row">
            <Button
              size="sm"
              kind="soft"
              disabled={!row || !provider || busy !== null}
              title={!row && pick ? t("modelProviders.roles.notInRegistry", { model: pick }) : undefined}
              onClick={() => row && provider && onProbe(provider, row)}
              testId={`v2-mp-role-probe-${r.role}`}
            >
              {t("modelProviders.roles.action.probe")}
            </Button>
            <Button
              size="sm"
              kind="primary"
              disabled={!dirty || busy !== null || (!pick && !r.allow_empty)}
              onClick={() => void changed(r, () => api.modelRoles.set(r.role, pick), "modelProviders.roles.toast.saved")}
              testId={`v2-mp-role-save-${r.role}`}
            >
              {t("modelProviders.roles.action.save")}
            </Button>
            <Button
              size="sm"
              kind="soft"
              disabled={r.overridden_by === null || busy !== null}
              onClick={() => void changed(r, () => api.modelRoles.reset(r.role), "modelProviders.roles.toast.reset")}
              testId={`v2-mp-role-reset-${r.role}`}
            >
              {t("modelProviders.roles.action.reset")}
            </Button>
          </span>
        );
      },
    },
  ];

  // N17-obs251: no actions column for a member
  const columns = readOnly ? allColumns.filter((c) => c.key !== "actions") : allColumns;

  return (
    <Card title={t("modelProviders.roles.title")} sub={t("modelProviders.roles.desc")} testId="v2-mp-roles">
      {readOnly && (
        <p className="v2-muted" data-testid="v2-mp-roles-readonly">
          {t("modelProviders.roles.readOnly")}
        </p>
      )}
      {defaults.error && <Alert tone="error">{t("modelProviders.roles.loadFailed", { msg: defaults.error })}</Alert>}
      {defaults.loading && !defaults.data ? (
        <Spin />
      ) : (
        <Table columns={columns} rows={roles} rowKey={(r) => r.role} testId="v2-mp-roles-table" />
      )}
      {/* N17-obs202: the system presets read these roles at install / repair time only */}
      <p className="v2-muted" data-testid="v2-mp-roles-preset-hint" style={{ marginTop: 12 }}>
        {t("modelProviders.roles.presetHint")}
      </p>
    </Card>
  );
}
