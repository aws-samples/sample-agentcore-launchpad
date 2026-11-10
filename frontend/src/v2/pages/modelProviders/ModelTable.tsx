import { Plus, Star } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { ModelProviderOut, ModelRowOut } from "../../../lib/api";
import { Button, type Column, LinkButton, Table, Tag } from "../../ui";
import type { useModelProviderActions } from "./actions";
import { type CapabilityKey, capabilityKeysFor, capabilityValue, fieldSource, SOURCE_TONE } from "./common";
import { Switch } from "./Switch";

type Actions = ReturnType<typeof useModelProviderActions>;

/** Capability chips (`tools ✓/✗`, `stream`, `vision`, `temp`, `top_p`, `max 16384`, `judge 4096`,
 *  `dim 1024`); each chip's tooltip names where the value came from (`capability_sources`). */
export function CapabilityChips({ model }: { model: ModelRowOut }) {
  const { t } = useTranslation();
  return (
    <div className="v2-mp-chips" data-testid={`v2-mp-caps-${model.model_id}`}>
      {capabilityKeysFor(model.kind).map((key) => {
        const value = capabilityValue(model, key);
        const source = fieldSource(model, key);
        const title = `${t(`modelProviders.cap.${key}`)} · ${t(`modelProviders.source.${source}`)}`;
        if (typeof value === "boolean" || value === null) {
          const on = value === true;
          return (
            <Tag key={key} tone={source === "unknown" ? "orange" : on ? "green" : "gray"} title={title} testId={`v2-mp-cap-${model.model_id}-${key}`}>
              {t(`modelProviders.cap.${key}`)} {source === "unknown" ? "?" : on ? "✓" : "✗"}
            </Tag>
          );
        }
        return (
          <Tag key={key} tone="outline" title={title} testId={`v2-mp-cap-${model.model_id}-${key}`}>
            {t(`modelProviders.capShort.${key}`)} {value.toLocaleString()}
          </Tag>
        );
      })}
    </div>
  );
}

/** `source` badge of a row: builtin / probed / manual (per-field detail in the chip tooltips). */
export function SourceTag({ model }: { model: ModelRowOut }) {
  const { t } = useTranslation();
  const unverified = Object.values(model.capability_sources ?? {}).includes("unknown");
  return (
    <span className="v2-row">
      <Tag tone={SOURCE_TONE[model.source] ?? "gray"} testId={`v2-mp-source-${model.model_id}`}>
        {t(`modelProviders.source.${model.source}`)}
      </Tag>
      {unverified && (
        <Tag tone="orange" title={t("modelProviders.unverifiedHint")}>
          {t("modelProviders.source.unknown")}
        </Tag>
      )}
    </span>
  );
}

/**
 * The selected provider's models (plan §8): Launchpad id, API model name, kind chip,
 * capability chips, source badge, enabled toggle, default star, Probe / Edit / Delete.
 */
export function ModelTable({
  provider,
  models,
  onAdd,
  onEdit,
  onProbe,
  loading,
  actions,
  readOnly = false,
  canSetDefault = true,
}: {
  provider: ModelProviderOut;
  models: ModelRowOut[];
  onAdd: () => void;
  onEdit: (m: ModelRowOut) => void;
  onProbe: (m: ModelRowOut) => void;
  loading: boolean;
  actions: Actions;
  /** N17-obs203: the provider is another creator's — no add / toggle / edit / delete / probe */
  readOnly?: boolean;
  /** the default star moves the platform default: administrators only */
  canSetDefault?: boolean;
}) {
  const { t } = useTranslation();
  const columns: Column<ModelRowOut>[] = [
    {
      key: "model",
      title: t("modelProviders.col.model"),
      render: (m) => (
        <span className="v2-mp-model">
          <b>{m.label}</b>
          <span className="id">{m.model_id}</span>
          {m.api_model_name !== m.model_id && <span className="id">{t("modelProviders.col.apiName")}: {m.api_model_name}</span>}
        </span>
      ),
    },
    {
      key: "kind",
      title: t("modelProviders.col.kind"),
      render: (m) => <Tag tone={m.kind === "embedding" ? "orange" : "blue"}>{t(`modelProviders.modelKind.${m.kind}`)}</Tag>,
    },
    { key: "caps", title: t("modelProviders.col.capabilities"), render: (m) => <CapabilityChips model={m} /> },
    { key: "source", title: t("modelProviders.col.source"), render: (m) => <SourceTag model={m} /> },
    {
      key: "enabled",
      title: t("modelProviders.col.enabled"),
      render: (m) => (
        <Switch
          on={m.enabled}
          disabled={actions.busyId === m.id || readOnly}
          label={t(m.enabled ? "modelProviders.action.disable" : "modelProviders.action.enable")}
          onChange={() => void actions.toggleModel(provider, m)}
          testId={`v2-mp-model-toggle-${m.model_id}`}
        />
      ),
    },
    {
      key: "default",
      title: t("modelProviders.col.default"),
      render: (m) => (
        <button
          type="button"
          className={m.is_default ? "v2-mp-star on" : "v2-mp-star"}
          aria-pressed={m.is_default}
          aria-label={t("modelProviders.action.setDefault")}
          title={m.is_default ? t("modelProviders.isDefault", { kind: t(`modelProviders.modelKind.${m.kind}`) }) : t("modelProviders.action.setDefault")}
          disabled={m.is_default || !m.enabled || actions.busyId === m.id || readOnly || !canSetDefault}
          onClick={() => void actions.setDefault(provider, m)}
          data-testid={`v2-mp-model-default-${m.model_id}`}
        >
          <Star size={15} aria-hidden="true" fill={m.is_default ? "currentColor" : "none"} />
        </button>
      ),
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (m) => {
        const busy = actions.busyId === m.id;
        if (readOnly) {
          return <span className="v2-muted">{t("v2.common.creatorOnly")}</span>;
        }
        return (
          <div className="v2-actions">
            <LinkButton disabled={busy} onClick={() => onProbe(m)} testId={`v2-mp-model-probe-${m.model_id}`}>
              {t("modelProviders.action.probe")}
            </LinkButton>
            <LinkButton disabled={busy} onClick={() => onEdit(m)} testId={`v2-mp-model-edit-${m.model_id}`}>
              {t("v2.common.edit")}
            </LinkButton>
            {!(m.source === "builtin" && provider.builtin) && (
              <LinkButton danger disabled={busy} onClick={() => actions.askDeleteModel(provider, m)} testId={`v2-mp-model-delete-${m.model_id}`}>
                {t("v2.common.delete")}
              </LinkButton>
            )}
          </div>
        );
      },
    },
  ];
  return (
    <>
      <div className="v2-toolbar">
        <span className="v2-count">{t("modelProviders.count.models", { count: models.length })}</span>
        <div className="end">
          {!readOnly && (
            <Button kind="primary" onClick={onAdd} testId="v2-mp-add-model">
              <Plus size={14} aria-hidden="true" />
              {t("modelProviders.action.addModel")}
            </Button>
          )}
        </div>
      </div>
      <Table
        columns={columns}
        rows={models}
        rowKey={(m) => m.id}
        loading={loading}
        empty={t("modelProviders.empty.models")}
        testId="v2-mp-models"
      />
    </>
  );
}

export type { CapabilityKey };
