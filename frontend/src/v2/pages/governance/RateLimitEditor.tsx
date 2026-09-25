import { Plus, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, type GovernanceRateMetric, type GovernanceRatePeriod } from "../../../lib/api";
import { governanceError, isGatewayReady } from "../../../lib/governance";
import {
  draftFromRateLimit,
  emptyDraft,
  emptyEntry,
  entriesFromDraft,
  type EntryDraft,
  isJwtClaimValid,
  RATE_LIMIT_FIXED_KEYS,
  RATE_LIMIT_JWT_PREFIX,
  RATE_LIMIT_MAX_DESCRIPTION,
  RATE_LIMIT_MAX_KEYS,
  RATE_LIMIT_METRICS,
  type RateLimitDraft,
  validateDraft,
  WILDCARD,
} from "../../../lib/governanceRateLimits";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Field, FlowHeader, LinkButton, Spin, Tag } from "../../ui";

const PERIODS: GovernanceRatePeriod[] = ["second", "minute"];

/** Create a rate limit (`limit` absent) or edit an existing one's entries + description. */
export function RateLimitEditor({ gatewayId, limitId }: { gatewayId: string; limitId: string | null }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const loaded = useLoad(
    () => Promise.all([api.getGovernanceGateway(gatewayId), api.listGovernanceRateLimits(gatewayId)]),
    `gov-rl-editor:${gatewayId}`,
  );
  const gateway = loaded.data?.[0] ?? null;
  const existing = useMemo(
    () => (limitId ? (loaded.data?.[1].rate_limits.find((limit) => limit.id === limitId) ?? null) : null),
    [loaded.data, limitId],
  );
  const [draft, setDraft] = useState<RateLimitDraft>(emptyDraft);
  const [hydrated, setHydrated] = useState(!limitId);
  const [claim, setClaim] = useState("");
  const [saving, setSaving] = useState(false);

  // editing keeps the AWS key set and re-hydrates every entry, once
  useEffect(() => {
    if (!hydrated && existing) {
      setDraft(draftFromRateLimit(existing));
      setHydrated(true);
    }
  }, [existing, hydrated]);

  const back = () => setParams({ view: "gateway", gateway: gatewayId, section: "rateLimits" });
  const creating = !limitId;
  const draftBlockers = useMemo(() => validateDraft(draft), [draft]);
  const blockers = draftBlockers.map((code) => t(`governance.rateLimits.blockers.${code}`));
  if (gateway && !gateway.managed) blockers.unshift(t("governance.blockers.notManaged"));
  if (gateway && !isGatewayReady(gateway)) blockers.unshift(t("governance.blockers.gatewayNotReady"));
  if (existing && existing.status.toUpperCase() !== "ACTIVE") {
    blockers.unshift(t("governance.rateLimits.blockers.statusNotActive", { status: existing.status }));
  }

  const addKey = (key: string) => {
    if (draft.dimensionKeys.includes(key) || draft.dimensionKeys.length >= RATE_LIMIT_MAX_KEYS) return;
    setDraft((current) => ({
      ...current,
      dimensionKeys: [...current.dimensionKeys, key],
      entries: current.entries.map((entry) => ({ ...entry, dimensions: { ...entry.dimensions, [key]: WILDCARD } })),
    }));
  };
  const removeKey = (key: string) =>
    setDraft((current) => ({ ...current, dimensionKeys: current.dimensionKeys.filter((item) => item !== key) }));
  const updateEntry = (index: number, patch: (entry: EntryDraft) => EntryDraft) =>
    setDraft((current) => ({ ...current, entries: current.entries.map((entry, i) => (i === index ? patch(entry) : entry)) }));

  const claimKey = `${RATE_LIMIT_JWT_PREFIX}${claim.trim()}`;
  const claimAddable =
    isJwtClaimValid(claim.trim()) && !draft.dimensionKeys.includes(claimKey) && draft.dimensionKeys.length < RATE_LIMIT_MAX_KEYS;
  const addClaim = () => {
    addKey(claimKey);
    setClaim("");
  };

  const save = async () => {
    if (saving || blockers.length > 0) return;
    setSaving(true);
    try {
      const entries = entriesFromDraft(draft);
      const description = draft.description.trim() || null;
      if (creating) {
        await api.createGovernanceRateLimit(gatewayId, { dimension_keys: draft.dimensionKeys, entries, description });
        toast("success", t("governance.rateLimits.created"));
      } else {
        await api.updateGovernanceRateLimit(gatewayId, limitId, { entries, description });
        toast("success", t("governance.rateLimits.updatedMsg"));
      }
      back();
    } catch (error) {
      toast("error", governanceError(error));
    } finally {
      setSaving(false);
    }
  };

  const title = creating ? t("v2.governance.rl.newTitle") : t("v2.governance.rl.editTitle", { id: limitId });
  const header = (
    <FlowHeader
      title={
        <span className="v2-row">
          {title}
          {gateway && <span className="v2-muted" style={{ fontWeight: 400, fontSize: 13 }}>{gateway.name}</span>}
        </span>
      }
      onBack={back}
      end={
        <>
          <Button onClick={back}>{t("v2.common.cancel")}</Button>
          <Button kind="primary" disabled={saving || !gateway || !hydrated || blockers.length > 0} onClick={() => void save()} testId="v2-governance-rl-save">
            {t("v2.common.save")}
          </Button>
        </>
      }
    />
  );

  if (loaded.error) {
    return (
      <>
        {header}
        <Alert tone="error" action={<LinkButton onClick={loaded.reload}>{t("v2.common.retry")}</LinkButton>}>
          {loaded.error}
        </Alert>
      </>
    );
  }
  if (!loaded.data) {
    return (
      <>
        {header}
        <Spin />
      </>
    );
  }
  if (limitId && !existing) {
    return (
      <>
        {header}
        <Alert tone="error">{t("v2.governance.rl.notFound", { id: limitId })}</Alert>
      </>
    );
  }

  return (
    <>
      {header}
      {blockers.length > 0 && (
        <Alert tone="warn">
          <span data-testid="rate-limit-blockers">
            {t("governance.rateLimits.blockedPrefix")} {blockers.join("; ")}
          </span>
        </Alert>
      )}
      <Alert>{t("governance.rateLimits.semantics")}</Alert>

      <Card title={t("v2.governance.rl.colKeys")} sub={creating ? t("governance.rateLimits.keysHelp") : t("governance.rateLimits.keysLocked")}>
        <div className="v2-tags" data-testid="rate-limit-keys">
          {draft.dimensionKeys.map((key, index) => (
            <Tag key={key} tone="blue">
              <span className="mono">
                {index + 1}. {key}
              </span>
              {creating && (
                <button
                  type="button"
                  className="v2-governance-tag-x"
                  aria-label={t("governance.rateLimits.removeKey", { key })}
                  onClick={() => removeKey(key)}
                >
                  <X size={12} aria-hidden="true" />
                </button>
              )}
            </Tag>
          ))}
          {draft.dimensionKeys.length === 0 && <span className="v2-muted">{t("governance.rateLimits.blockers.noKeys")}</span>}
        </div>
        {creating && (
          <>
            <div className="v2-sub-title">{t("v2.governance.rl.pickKey")}</div>
            <div className="v2-tags">
              {RATE_LIMIT_FIXED_KEYS.filter((key) => !draft.dimensionKeys.includes(key)).map((key) => (
                <Button key={key} size="sm" disabled={draft.dimensionKeys.length >= RATE_LIMIT_MAX_KEYS} onClick={() => addKey(key)}>
                  <Plus size={12} aria-hidden="true" />
                  <span className="mono">{key}</span>
                </Button>
              ))}
            </div>
            <div className="v2-governance-claim">
              <span className="mono v2-muted">{RATE_LIMIT_JWT_PREFIX}</span>
              <input
                className="v2-input mono"
                aria-label={t("v2.governance.rl.jwtClaim")}
                placeholder={t("governance.rateLimits.jwtPlaceholder")}
                value={claim}
                onChange={(e) => setClaim(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && claimAddable) {
                    e.preventDefault();
                    addClaim();
                  }
                }}
              />
              <Button size="sm" disabled={!claimAddable} onClick={addClaim}>
                {t("v2.governance.rl.addKey")}
              </Button>
            </div>
          </>
        )}
      </Card>

      <Card
        title={t("v2.governance.rl.entries")}
        sub={t("governance.rateLimits.metricsHelp")}
        end={
          <Button
            size="sm"
            disabled={draft.dimensionKeys.length === 0}
            title={draft.dimensionKeys.length === 0 ? t("governance.rateLimits.blockers.noKeys") : undefined}
            onClick={() => setDraft((current) => ({ ...current, entries: [...current.entries, emptyEntry(current.dimensionKeys)] }))}
            testId="v2-governance-rl-add-entry"
          >
            <Plus size={14} aria-hidden="true" />
            {t("v2.governance.rl.addEntry")}
          </Button>
        }
      >
        {draft.entries.length === 0 ? (
          <div className="v2-muted">{t("governance.rateLimits.blockers.noEntries")}</div>
        ) : (
          <div className="v2-stack" style={{ gap: 12 }}>
            {draft.entries.map((entry, index) => (
              <div key={index} className="v2-governance-entry" data-testid={`rate-limit-entry-${index}`}>
                <div className="v2-governance-entry-head">
                  <b>{t("v2.governance.rl.entry", { n: index + 1 })}</b>
                  <span className="v2-muted">{t("governance.rateLimits.wildcardHint")}</span>
                  <LinkButton
                    danger
                    onClick={() => setDraft((current) => ({ ...current, entries: current.entries.filter((_, i) => i !== index) }))}
                  >
                    {t("v2.governance.rl.removeEntry")}
                  </LinkButton>
                </div>
                <div className="v2-form cols-2">
                  {draft.dimensionKeys.map((key) => (
                    <Field key={key} label={<span className="mono">{key}</span>}>
                      <input
                        className="v2-input mono"
                        value={entry.dimensions[key] ?? ""}
                        placeholder={WILDCARD}
                        onChange={(e) =>
                          updateEntry(index, (current) => ({ ...current, dimensions: { ...current.dimensions, [key]: e.target.value } }))
                        }
                      />
                    </Field>
                  ))}
                </div>
                <div className="v2-governance-metrics">
                  {RATE_LIMIT_METRICS.map((metric: GovernanceRateMetric) => {
                    const config = entry[metric];
                    return (
                      <div key={metric} className="v2-governance-metric">
                        <label className="v2-check">
                          <input
                            type="checkbox"
                            checked={config.enabled}
                            onChange={(e) =>
                              updateEntry(index, (current) => ({ ...current, [metric]: { ...current[metric], enabled: e.target.checked } }))
                            }
                          />
                          <span className="mono">{metric}</span>
                        </label>
                        <input
                          className="v2-input mono"
                          type="number"
                          min={0}
                          max={10_000_000}
                          step="any"
                          aria-label={`${metric} ${t("v2.governance.rl.rate")}`}
                          disabled={!config.enabled}
                          value={config.rate}
                          placeholder={t("v2.governance.rl.rate")}
                          onChange={(e) =>
                            updateEntry(index, (current) => ({ ...current, [metric]: { ...current[metric], rate: e.target.value } }))
                          }
                        />
                        <select
                          className="v2-select"
                          aria-label={`${metric} ${t("v2.governance.rl.period")}`}
                          disabled={!config.enabled}
                          value={config.period}
                          onChange={(e) =>
                            updateEntry(index, (current) => ({
                              ...current,
                              [metric]: { ...current[metric], period: e.target.value as GovernanceRatePeriod },
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
        )}
      </Card>

      <Card>
        <Field label={t("v2.governance.description")} hint={`${draft.description.length} / ${RATE_LIMIT_MAX_DESCRIPTION}`}>
          <input
            className="v2-input"
            maxLength={RATE_LIMIT_MAX_DESCRIPTION}
            placeholder={t("governance.rateLimits.descriptionPlaceholder")}
            value={draft.description}
            onChange={(e) => setDraft((current) => ({ ...current, description: e.target.value }))}
          />
        </Field>
      </Card>
    </>
  );
}
