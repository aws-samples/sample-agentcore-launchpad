import { Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api, errorMessage, type EvaluatorRow, type OnlineEvalConfigRow, type OnlineEvalMode } from "../../../lib/api";
import { EvaluatorPicker } from "../../EvaluatorPicker";
import { useLoad, useV2Toast } from "../../hooks";
import {
  createBody,
  draftFromRow,
  eligibleAgent,
  emptyFilter,
  type FilterDraft,
  type FilterKind,
  FREQUENCIES,
  INSIGHT_TYPES,
  insightLabel,
  isEditable,
  isTransient,
  MAX_EVALUATORS,
  MAX_FILTERS,
  modeOf,
  newDraft,
  type OnlineDraft,
  OPERATORS,
  patchBody,
  validateDraft,
  withMode,
} from "../../online";
import { Alert, Button, Card, Field, FlowHeader, OptionCard, Spin } from "../../ui";

function FilterRows({ filters, onChange }: { filters: FilterDraft[]; onChange: (next: FilterDraft[]) => void }) {
  const { t } = useTranslation();
  const set = (i: number, patch: Partial<FilterDraft>) => onChange(filters.map((f, j) => (j === i ? { ...f, ...patch } : f)));
  return (
    <div className="v2-stack">
      {filters.map((f, i) => (
        <div key={i} className="v2-row" style={{ flexWrap: "nowrap" }}>
          <input
            className="v2-input mono"
            style={{ flex: 2 }}
            value={f.key}
            placeholder="session.id"
            aria-label={t("v2.online.filterKey")}
            onChange={(e) => set(i, { key: e.target.value })}
          />
          <select className="v2-select" style={{ flex: 1.4 }} value={f.operator} aria-label={t("v2.online.filterOperator")} onChange={(e) => set(i, { operator: e.target.value as FilterDraft["operator"] })}>
            {OPERATORS.map((op) => (
              <option key={op} value={op}>
                {op}
              </option>
            ))}
          </select>
          <select
            className="v2-select"
            style={{ flex: 1 }}
            value={f.kind}
            aria-label={t("v2.online.filterKind")}
            onChange={(e) => {
              const kind = e.target.value as FilterKind;
              set(i, { kind, value: kind === "boolean" ? "true" : "" });
            }}
          >
            {(["string", "number", "boolean"] as FilterKind[]).map((k) => (
              <option key={k} value={k}>
                {t(`v2.online.kind.${k}`)}
              </option>
            ))}
          </select>
          {f.kind === "boolean" ? (
            <select className="v2-select" style={{ flex: 2 }} value={f.value} aria-label={t("v2.online.filterValue")} onChange={(e) => set(i, { value: e.target.value })}>
              <option value="true">true</option>
              <option value="false">false</option>
            </select>
          ) : (
            <input
              className="v2-input"
              style={{ flex: 2 }}
              type={f.kind === "number" ? "number" : "text"}
              value={f.value}
              aria-label={t("v2.online.filterValue")}
              onChange={(e) => set(i, { value: e.target.value })}
            />
          )}
          <Button size="sm" onClick={() => onChange(filters.filter((_, j) => j !== i))} title={t("v2.common.delete")}>
            <Trash2 size={13} aria-hidden="true" />
          </Button>
        </div>
      ))}
      <div>
        <Button size="sm" disabled={filters.length >= MAX_FILTERS} onClick={() => onChange([...filters, emptyFilter()])}>
          <Plus size={13} aria-hidden="true" />
          {t("v2.online.addFilter")}
        </Button>
      </div>
    </div>
  );
}

function toggleOrdered<T extends string>(all: readonly T[], picked: T[], value: T): T[] {
  // canonical order, whatever the click order
  return all.filter((x) => (x === value ? !picked.includes(x) : picked.includes(x)));
}

/** Create (`id` null) or edit an agent-owned online evaluation config. */
export function OnlineEditor({ id }: { id: string | null }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const { can } = useAuth();
  const editing = id !== null;
  const existing = useLoad<OnlineEvalConfigRow | null>(() => (id ? api.v2OnlineConfig(id) : Promise.resolve(null)), `online-edit:${id}`);
  const agents = useLoad(() => api.listAgents(), "agents");
  const evaluators = useLoad(() => api.v2Evaluators(), "evaluators");
  const [draft, setDraft] = useState<OnlineDraft>(newDraft);
  const [agentId, setAgentId] = useState("");
  const [enable, setEnable] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (patch: Partial<OnlineDraft>) => setDraft((prev) => ({ ...prev, ...patch }));

  const row = existing.data;
  useEffect(() => {
    if (row) setDraft(draftFromRow(row));
  }, [row]);

  const eligible = useMemo(() => (agents.data?.agents ?? []).filter(eligibleAgent), [agents.data]);
  const mode: OnlineEvalMode = row ? modeOf(row) : draft.mode;
  const patch = row ? patchBody(row, draft) : null;
  const dirty = patch ? Object.keys(patch).length > 0 : true;

  // live traffic carries no ground truth: evaluators that need it cannot judge it
  const blocked = (e: EvaluatorRow) =>
    e.requires_ground_truth || e.id.startsWith("Builtin.Trajectory") ? t("v2.online.needsGt") : null;

  const submit = async () => {
    setError(null);
    if (!editing && !agentId) return setError(t("v2.online.err.agent"));
    const problem = validateDraft(t, draft);
    if (problem) return setError(problem);
    setBusy(true);
    try {
      if (row && patch) {
        await api.v2UpdateOnlineConfig(row.config_id, patch);
        toast("success", t("v2.online.saved"));
        setParams({ view: "detail", id: row.config_id });
      } else {
        const created = await api.v2CreateOnlineConfig(createBody(agentId, draft, enable));
        toast("success", t("v2.online.created"));
        setParams({ view: "detail", id: created.config_id });
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  if (editing && existing.loading && !row) return <Spin />;
  if (editing && existing.error && !row) return <Alert tone="error">{existing.error}</Alert>;
  if (row && !isEditable(row)) {
    return (
      <>
        <FlowHeader title={t("v2.online.editTitle")} onBack={() => setParams({ view: "detail", id: row.config_id })} />
        <Alert tone="warn">{t("v2.online.notEditable")}</Alert>
      </>
    );
  }

  return (
    <>
      <FlowHeader
        title={editing ? t("v2.online.editTitle") : t("v2.online.new")}
        onBack={() => setParams(row ? { view: "detail", id: row.config_id } : {})}
        end={
          <Button
            kind="primary"
            disabled={busy || !can("eval.run") || !dirty || (row ? isTransient(row) : false)}
            onClick={() => void submit()}
            testId="v2-online-submit"
          >
            {editing ? t("v2.common.save") : t("v2.online.create")}
          </Button>
        }
      />
      {error && <Alert tone="error">{error}</Alert>}
      <Card title={t("v2.tasks.basic")}>
        <div className="v2-form cols-2">
          <Field label={t("v2.tasks.colAgent")} required={!editing} hint={editing ? t("v2.online.agentFixed") : t("v2.online.agentHint")}>
            {editing ? (
              <input className="v2-input" value={row?.agent_name ?? row?.agent_id ?? "—"} readOnly />
            ) : (
              <select className="v2-select" value={agentId} onChange={(e) => setAgentId(e.target.value)} data-testid="v2-online-agent">
                <option value="">{t("v2.common.choose")}</option>
                {eligible.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name} · {a.method}
                  </option>
                ))}
              </select>
            )}
          </Field>
          <Field label={t("v2.tasks.description")} hint={t("v2.online.descHint")}>
            <input className="v2-input" maxLength={200} value={draft.description} onChange={(e) => set({ description: e.target.value })} data-testid="v2-online-desc" />
          </Field>
          <Field label={t("v2.online.colMode")} full hint={editing ? t("v2.online.modeFixed") : undefined}>
            <div className="v2-options" style={{ gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }}>
              {(["scores", "insights"] as OnlineEvalMode[]).map((m) => (
                <OptionCard
                  key={m}
                  title={t(`v2.online.mode.${m}`)}
                  desc={t(`v2.online.modeDesc.${m}`)}
                  on={mode === m}
                  disabled={editing && mode !== m}
                  onClick={() => !editing && setDraft((prev) => withMode(prev, m))}
                  testId={`v2-online-mode-${m}`}
                />
              ))}
            </div>
          </Field>
          {!editing && (
            <Field label={t("v2.online.enableOnCreate")}>
              <label className="v2-check">
                <input type="checkbox" checked={enable} onChange={(e) => setEnable(e.target.checked)} />
                {t("v2.online.enableOnCreateHint")}
              </label>
            </Field>
          )}
        </div>
      </Card>

      {mode === "scores" ? (
        <Card title={t("v2.tasks.pickEvaluators")} sub={t("v2.online.evaluatorsSub", { max: MAX_EVALUATORS })}>
          <EvaluatorPicker
            evaluators={evaluators.data?.evaluators ?? []}
            loading={evaluators.loading}
            error={evaluators.error}
            onRetry={evaluators.reload}
            selected={draft.evaluators}
            onChange={(next) => set({ evaluators: next })}
            max={MAX_EVALUATORS}
            blockedReason={blocked}
            testIdPrefix="v2-online-eval"
          />
        </Card>
      ) : (
        <Card title={t("v2.online.insightsTitle")} sub={t("v2.online.insightsSub")}>
          <div className="v2-form">
            <Field label={t("v2.online.insightTypes")}>
              <div className="v2-checks">
                {INSIGHT_TYPES.map((id) => (
                  <label key={id} className="v2-check">
                    <input
                      type="checkbox"
                      checked={draft.insights.includes(id)}
                      onChange={() => set({ insights: toggleOrdered(INSIGHT_TYPES, draft.insights as (typeof INSIGHT_TYPES)[number][], id) })}
                    />
                    {insightLabel(t, id)}
                  </label>
                ))}
              </div>
            </Field>
            <Field label={t("v2.online.frequencies")} hint={t("v2.online.frequenciesHint")}>
              <div className="v2-checks">
                {FREQUENCIES.map((f) => (
                  <label key={f} className="v2-check">
                    <input type="checkbox" checked={draft.frequencies.includes(f)} onChange={() => set({ frequencies: toggleOrdered(FREQUENCIES, draft.frequencies, f) })} />
                    {t(`v2.online.freq.${f}`)}
                  </label>
                ))}
              </div>
            </Field>
          </div>
        </Card>
      )}

      <Card title={t("v2.online.samplingTitle")}>
        <div className="v2-form cols-2">
          <Field label={t("v2.tasks.samplingRate")} hint={t("v2.tasks.samplingHint")}>
            <input
              className="v2-input"
              type="number"
              min={0.01}
              max={100}
              step="any"
              value={draft.sampling}
              onChange={(e) => set({ sampling: e.target.value, samplingTouched: true })}
              data-testid="v2-online-sampling"
            />
          </Field>
          <Field label={t("v2.tasks.sessionTimeout")} hint={t("v2.online.timeoutHint")}>
            <input className="v2-input" type="number" min={1} max={1440} step={1} value={draft.timeout} onChange={(e) => set({ timeout: e.target.value })} />
          </Field>
          <Field label={t("v2.online.filters")} full hint={t("v2.online.filtersHint", { max: MAX_FILTERS })}>
            <FilterRows filters={draft.filters} onChange={(filters) => set({ filters })} />
          </Field>
        </div>
      </Card>
    </>
  );
}
