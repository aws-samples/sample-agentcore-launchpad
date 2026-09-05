import { ChevronDown, ChevronRight, Gauge, Pencil, Plus, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Chip, ConfirmDialog, DataTable, Panel, useToast } from "../../components";
import {
  api,
  type GovernanceGatewayDetail,
  type GovernanceRateLimit,
  type GovernanceRateMetric,
  type GovernanceRatePeriod,
} from "../../lib/api";
import {
  RATE_LIMIT_FIXED_KEYS,
  RATE_LIMIT_JWT_PREFIX,
  RATE_LIMIT_MAX_DESCRIPTION,
  RATE_LIMIT_MAX_KEYS,
  RATE_LIMIT_METRICS,
  draftFromRateLimit,
  emptyDraft,
  emptyEntry,
  entriesFromDraft,
  entryMetricSummary,
  isJwtClaimValid,
  validateDraft,
  type EntryDraft,
  type RateLimitDraft,
} from "./rateLimits";
import { formatTimestamp, governanceError, isGatewayReady, statusTone } from "./types";

interface Props {
  gateway: GovernanceGatewayDetail;
  /** Another governance operation is in flight on this Gateway. */
  operationBusy: boolean;
  /** Bumped by the parent's REFRESH so the list reloads with the rest of the page. */
  refreshTick: number;
}

type FormState = { mode: "create" } | { mode: "edit"; limit: GovernanceRateLimit };

const PERIODS: GovernanceRatePeriod[] = ["second", "minute"];

export function RateLimitsPanel({ gateway, operationBusy, refreshTick }: Props) {
  const { t, i18n } = useTranslation();
  const toast = useToast();
  const [limits, setLimits] = useState<GovernanceRateLimit[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [form, setForm] = useState<FormState | null>(null);
  const [draft, setDraft] = useState<RateLimitDraft>(emptyDraft);
  const [claim, setClaim] = useState("");
  const [saving, setSaving] = useState(false);
  const [toDelete, setToDelete] = useState<GovernanceRateLimit | null>(null);

  const load = useCallback(async () => {
    try {
      const result = await api.listGovernanceRateLimits(gateway.id);
      setLimits(result.rate_limits);
      setLoadError(null);
    } catch (error) {
      setLimits((current) => current ?? []);
      setLoadError(governanceError(error));
    }
  }, [gateway.id]);

  useEffect(() => {
    void load();
  }, [load, refreshTick]);

  // Mutation gates, in the order the operator can fix them.
  const gatewayBlockers: string[] = [];
  if (!gateway.managed) gatewayBlockers.push(t("governance.blockers.notManaged"));
  if (!isGatewayReady(gateway)) gatewayBlockers.push(t("governance.blockers.gatewayNotReady"));
  if (operationBusy) gatewayBlockers.push(t("governance.blockers.busy"));
  const addBlockers = [...gatewayBlockers];
  if (form) addBlockers.push(t("governance.rateLimits.blockers.formOpen"));

  const rowBlockers = (limit: GovernanceRateLimit): string[] => {
    const blockers = [...addBlockers];
    if (limit.status.toUpperCase() !== "ACTIVE") {
      blockers.push(t("governance.rateLimits.blockers.statusNotActive", { status: limit.status }));
    }
    return blockers;
  };

  const draftBlockers = useMemo(() => validateDraft(draft), [draft]);
  const saveBlockers = draftBlockers.map((code) => t(`governance.rateLimits.blockers.${code}`));
  const saveDisabled = saving || saveBlockers.length > 0;

  const openCreate = () => {
    setDraft(emptyDraft());
    setClaim("");
    setForm({ mode: "create" });
  };
  const openEdit = (limit: GovernanceRateLimit) => {
    setDraft(draftFromRateLimit(limit));
    setClaim("");
    setForm({ mode: "edit", limit });
  };
  const closeForm = () => setForm(null);

  const addKey = (key: string) => {
    if (draft.dimensionKeys.includes(key) || draft.dimensionKeys.length >= RATE_LIMIT_MAX_KEYS) {
      return;
    }
    setDraft((current) => ({
      ...current,
      dimensionKeys: [...current.dimensionKeys, key],
      entries: current.entries.map((entry) => ({
        ...entry,
        dimensions: { ...entry.dimensions, [key]: "*" },
      })),
    }));
  };
  const removeKey = (key: string) => {
    setDraft((current) => ({
      ...current,
      dimensionKeys: current.dimensionKeys.filter((item) => item !== key),
    }));
  };
  const updateEntry = (index: number, patch: (entry: EntryDraft) => EntryDraft) => {
    setDraft((current) => ({
      ...current,
      entries: current.entries.map((entry, i) => (i === index ? patch(entry) : entry)),
    }));
  };

  const save = async () => {
    if (!form || saveDisabled) return;
    setSaving(true);
    try {
      const entries = entriesFromDraft(draft);
      const description = draft.description.trim() || null;
      if (form.mode === "create") {
        await api.createGovernanceRateLimit(gateway.id, {
          dimension_keys: draft.dimensionKeys,
          entries,
          description,
        });
        toast(t("governance.rateLimits.created"), "good");
      } else {
        await api.updateGovernanceRateLimit(gateway.id, form.limit.id, { entries, description });
        toast(t("governance.rateLimits.updatedMsg"), "good");
      }
      setForm(null);
      await load();
    } catch (error) {
      toast(governanceError(error), "crit");
    } finally {
      setSaving(false);
    }
  };

  const confirmDelete = async () => {
    if (!toDelete) return;
    const limit = toDelete;
    setToDelete(null);
    setSaving(true);
    try {
      await api.deleteGovernanceRateLimit(gateway.id, limit.id);
      toast(t("governance.rateLimits.deleted"), "good");
      await load();
    } catch (error) {
      toast(governanceError(error), "crit");
    } finally {
      setSaving(false);
    }
  };

  const toggleExpanded = (id: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const claimKey = `${RATE_LIMIT_JWT_PREFIX}${claim.trim()}`;
  const claimAddable =
    isJwtClaimValid(claim.trim()) &&
    !draft.dimensionKeys.includes(claimKey) &&
    draft.dimensionKeys.length < RATE_LIMIT_MAX_KEYS;

  return (
    <Panel
      title={t("governance.rateLimits.title")}
      sub={loadError ?? t("governance.rateLimits.sub")}
      pad={false}
      end={
        <Btn
          primary
          disabled={addBlockers.length > 0}
          disabledReason={addBlockers.join("; ")}
          onClick={openCreate}
        >
          <Plus size={14} aria-hidden="true" />
          {t("governance.rateLimits.add")}
        </Btn>
      }
    >
      <div className="gov-rl-section">
        <div className="gov-alert" role="note">
          <Gauge size={15} aria-hidden="true" />
          <span>{t("governance.rateLimits.semantics")}</span>
        </div>
      </div>

      <DataTable
        columns={[
          { key: "id", label: t("governance.rateLimits.id") },
          { key: "keys", label: t("governance.rateLimits.keys") },
          { key: "entries", label: t("governance.rateLimits.entryCount") },
          { key: "status", label: t("governance.inventory.status") },
          { key: "updated", label: t("governance.rateLimits.updated") },
          { key: "actions", label: "" },
        ]}
        isEmpty={(limits?.length ?? 0) === 0}
        empty={loadError ?? (limits === null ? t("common.loading") : t("governance.rateLimits.empty"))}
        error={loadError}
        onRetry={() => void load()}
      >
        {limits?.map((limit) => {
          const open = expanded.has(limit.id);
          const blockers = rowBlockers(limit);
          return [
            <tr key={limit.id} data-testid={`rate-limit-${limit.id}`}>
              <td className="pri">
                <button
                  type="button"
                  className="gov-rl-toggle mono"
                  aria-expanded={open}
                  aria-label={t(open ? "governance.rateLimits.collapse" : "governance.rateLimits.expand")}
                  onClick={() => toggleExpanded(limit.id)}
                >
                  {open ? (
                    <ChevronDown size={13} aria-hidden="true" />
                  ) : (
                    <ChevronRight size={13} aria-hidden="true" />
                  )}
                  {limit.id}
                </button>
                {limit.description ? (
                  <div className="gov-cell-note">{limit.description}</div>
                ) : null}
              </td>
              <td>
                <div className="gov-action-list">
                  {limit.dimension_keys.map((key) => (
                    <Chip key={key} tone="muted">
                      {key}
                    </Chip>
                  ))}
                </div>
              </td>
              <td className="mono">{limit.entries.length}</td>
              <td>
                <Chip tone={statusTone(limit.status)}>{limit.status}</Chip>
              </td>
              <td className="mono">{formatTimestamp(limit.updated_at, i18n.language)}</td>
              <td>
                <div className="gov-action-list">
                  <Btn
                    disabled={blockers.length > 0}
                    disabledReason={blockers.join("; ")}
                    onClick={() => openEdit(limit)}
                  >
                    <Pencil size={14} aria-hidden="true" />
                    {t("governance.rateLimits.edit")}
                  </Btn>
                  <Btn
                    disabled={blockers.length > 0}
                    disabledReason={blockers.join("; ")}
                    onClick={() => setToDelete(limit)}
                  >
                    <Trash2 size={14} aria-hidden="true" />
                    {t("governance.rateLimits.delete")}
                  </Btn>
                </div>
              </td>
            </tr>,
            open ? (
              <tr key={`${limit.id}-entries`} className="gov-rl-entries-row">
                <td colSpan={6}>
                  <table className="gov-rl-entries">
                    <thead>
                      <tr>
                        {limit.dimension_keys.map((key) => (
                          <th key={key}>{key}</th>
                        ))}
                        <th>{t("governance.rateLimits.rate")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {limit.entries.map((entry, index) => (
                        <tr key={index}>
                          {limit.dimension_keys.map((key) => (
                            <td key={key} className="mono">
                              {entry.dimensions[key] ?? "-"}
                            </td>
                          ))}
                          <td>
                            <div className="gov-action-list">
                              {entryMetricSummary(entry).map((label) => (
                                <Chip key={label} tone="muted">
                                  {label}
                                </Chip>
                              ))}
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </td>
              </tr>
            ) : null,
          ];
        })}
      </DataTable>

      {form ? (
        <div className="gov-rl-section gov-rl-form" data-testid="rate-limit-form">
          <div className="gov-rl-form-head">
            <strong>
              {form.mode === "create"
                ? t("governance.rateLimits.newTitle")
                : t("governance.rateLimits.editTitle", { id: form.limit.id })}
            </strong>
            <button
              type="button"
              className="gov-icon-button"
              aria-label={t("common.cancel")}
              onClick={closeForm}
            >
              <X size={14} aria-hidden="true" />
            </button>
          </div>

          <div className="field">
            <label>{t("governance.rateLimits.keys")}</label>
            <div className="selchips" data-testid="rate-limit-keys">
              {draft.dimensionKeys.map((key) => (
                <span key={key} className="selchip on mono">
                  {key}
                  {form.mode === "create" ? (
                    <button
                      type="button"
                      className="gov-icon-button"
                      aria-label={t("governance.rateLimits.removeKey", { key })}
                      onClick={() => removeKey(key)}
                    >
                      <X size={11} aria-hidden="true" />
                    </button>
                  ) : null}
                </span>
              ))}
              {draft.dimensionKeys.length === 0 ? (
                <span className="gov-cell-note">{t("governance.rateLimits.blockers.noKeys")}</span>
              ) : null}
            </div>
            {form.mode === "create" ? (
              <>
                <div className="selchips gov-rl-key-picker">
                  {RATE_LIMIT_FIXED_KEYS.filter((key) => !draft.dimensionKeys.includes(key)).map(
                    (key) => (
                      <button
                        key={key}
                        type="button"
                        className="selchip mono"
                        disabled={draft.dimensionKeys.length >= RATE_LIMIT_MAX_KEYS}
                        onClick={() => addKey(key)}
                      >
                        <Plus size={11} aria-hidden="true" />
                        {key}
                      </button>
                    ),
                  )}
                </div>
                <div className="gov-inline-field gov-rl-claim">
                  <span className="mono gov-rl-prefix">{RATE_LIMIT_JWT_PREFIX}</span>
                  <input
                    className="input mono"
                    aria-label={t("governance.rateLimits.jwtClaim")}
                    placeholder={t("governance.rateLimits.jwtPlaceholder")}
                    value={claim}
                    onChange={(event) => setClaim(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" && claimAddable) {
                        event.preventDefault();
                        addKey(claimKey);
                        setClaim("");
                      }
                    }}
                  />
                  <Btn
                    disabled={!claimAddable}
                    onClick={() => {
                      addKey(claimKey);
                      setClaim("");
                    }}
                  >
                    {t("governance.rateLimits.addKey")}
                  </Btn>
                </div>
                <div className="gov-field-help">{t("governance.rateLimits.keysHelp")}</div>
              </>
            ) : (
              <div className="gov-field-help">{t("governance.rateLimits.keysLocked")}</div>
            )}
          </div>

          <div className="field">
            <label>{t("governance.rateLimits.entryCount")}</label>
            <div className="gov-rl-entry-list">
              {draft.entries.map((entry, index) => (
                <div key={index} className="gov-rl-entry" data-testid={`rate-limit-entry-${index}`}>
                  <div className="gov-rl-form-head">
                    <span className="mono">{t("governance.rateLimits.entry", { n: index + 1 })}</span>
                    <Btn
                      onClick={() =>
                        setDraft((current) => ({
                          ...current,
                          entries: current.entries.filter((_, i) => i !== index),
                        }))
                      }
                    >
                      <Trash2 size={12} aria-hidden="true" />
                      {t("governance.rateLimits.removeEntry")}
                    </Btn>
                  </div>
                  <div className="gov-rl-dimensions">
                    {draft.dimensionKeys.map((key) => (
                      <label key={key} className="gov-rl-dimension">
                        <span className="mono">{key}</span>
                        <input
                          className="input mono"
                          value={entry.dimensions[key] ?? ""}
                          placeholder="*"
                          onChange={(event) =>
                            updateEntry(index, (current) => ({
                              ...current,
                              dimensions: { ...current.dimensions, [key]: event.target.value },
                            }))
                          }
                        />
                      </label>
                    ))}
                  </div>
                  <div className="gov-rl-metrics">
                    {RATE_LIMIT_METRICS.map((metric: GovernanceRateMetric) => {
                      const config = entry[metric];
                      return (
                        <div key={metric} className="gov-rl-metric">
                          <label className="gov-check-row">
                            <input
                              type="checkbox"
                              checked={config.enabled}
                              onChange={(event) =>
                                updateEntry(index, (current) => ({
                                  ...current,
                                  [metric]: { ...current[metric], enabled: event.target.checked },
                                }))
                              }
                            />
                            <span>{t(`governance.rateLimits.metric_${metric}`)}</span>
                          </label>
                          <input
                            className="input mono"
                            type="number"
                            min={0}
                            max={10_000_000}
                            step="any"
                            aria-label={`${metric} ${t("governance.rateLimits.rate")}`}
                            disabled={!config.enabled}
                            value={config.rate}
                            onChange={(event) =>
                              updateEntry(index, (current) => ({
                                ...current,
                                [metric]: { ...current[metric], rate: event.target.value },
                              }))
                            }
                          />
                          <select
                            className="input mono"
                            aria-label={`${metric} ${t("governance.rateLimits.period")}`}
                            disabled={!config.enabled}
                            value={config.period}
                            onChange={(event) =>
                              updateEntry(index, (current) => ({
                                ...current,
                                [metric]: {
                                  ...current[metric],
                                  period: event.target.value as GovernanceRatePeriod,
                                },
                              }))
                            }
                          >
                            {PERIODS.map((period) => (
                              <option key={period} value={period}>
                                {t(`governance.rateLimits.period_${period}`)}
                              </option>
                            ))}
                          </select>
                        </div>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
            <div className="gov-actions">
              <Btn
                disabled={draft.dimensionKeys.length === 0}
                disabledReason={t("governance.rateLimits.blockers.noKeys")}
                onClick={() =>
                  setDraft((current) => ({
                    ...current,
                    entries: [...current.entries, emptyEntry(current.dimensionKeys)],
                  }))
                }
              >
                <Plus size={14} aria-hidden="true" />
                {t("governance.rateLimits.addEntry")}
              </Btn>
              <span className="gov-cell-note">{t("governance.rateLimits.wildcardHint")}</span>
            </div>
            <div className="gov-field-help">{t("governance.rateLimits.metricsHelp")}</div>
          </div>

          <div className="field">
            <label>{t("governance.rateLimits.description")}</label>
            <input
              className="input"
              maxLength={RATE_LIMIT_MAX_DESCRIPTION}
              placeholder={t("governance.rateLimits.descriptionPlaceholder")}
              value={draft.description}
              onChange={(event) =>
                setDraft((current) => ({ ...current, description: event.target.value }))
              }
            />
          </div>

          {saveBlockers.length > 0 ? (
            <div className="gov-inline-error" data-testid="rate-limit-blockers">
              {t("governance.rateLimits.blockedPrefix")} {saveBlockers.join("; ")}
            </div>
          ) : null}
          <div className="gov-actions">
            <Btn
              primary
              disabled={saveDisabled}
              disabledReason={saveBlockers.join("; ")}
              onClick={() => void save()}
            >
              {t("governance.rateLimits.save")}
            </Btn>
            <Btn onClick={closeForm}>{t("common.cancel")}</Btn>
          </div>
        </div>
      ) : null}

      <ConfirmDialog
        open={toDelete !== null}
        title={t("governance.rateLimits.confirmTitle")}
        body={t("governance.rateLimits.confirmDelete", {
          id: toDelete?.id ?? "",
          name: gateway.name,
        })}
        confirmLabel={t("governance.rateLimits.delete")}
        onConfirm={() => void confirmDelete()}
        onCancel={() => setToDelete(null)}
      />
    </Panel>
  );
}
