import "../online.css";

import { ArrowLeft, Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { api, type EvaluatorRow, type OnlineEvalConfigRow, type OnlineEvalMode } from "../../../lib/api";
import { EvaluatorPicker } from "../../../v2/EvaluatorPicker";
import {
  copyDraft,
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
} from "../../../v2/online";
import { V2ToastProvider } from "../../../v2/ui";
import { useWorkspace } from "../../../workspace/workspace-context";
import { evalCostApi, type EvalCostEstimate } from "../../../lib/api/evalCost";
import { useLoad, useToast } from "../../hooks";
import { permReason } from "../../reasons";
import { Reasoned } from "../../reason";
import { Btn, Notice, PageHead, Panel, Skeleton } from "../../ui";
import { costRefusal, refusalText } from "../../../lib/eval-cost-refusal";
import { EstimateCostCard } from "../tasks/CostCard";
import { StrategySwitch } from "../tasks/StrategySwitch";
import { onlineHref } from "./actions";
import { Select } from "../../select";

/** canonical order, whatever the click order */
function toggleOrdered<T extends string>(all: readonly T[], picked: T[], value: T): T[] {
  return all.filter((x) => (x === value ? !picked.includes(x) : picked.includes(x)));
}

function FilterRows({ filters, onChange }: { filters: FilterDraft[]; onChange: (next: FilterDraft[]) => void }) {
  const { t } = useTranslation();
  const set = (i: number, patch: Partial<FilterDraft>) => onChange(filters.map((f, j) => (j === i ? { ...f, ...patch } : f)));
  return (
    <div style={{ display: "grid", gap: 8 }}>
      {filters.map((f, i) => (
        <div key={i} className="v3-onl-filter">
          <input className="v3-input mono" value={f.key} placeholder="session.id" aria-label={t("v2.online.filterKey")}
            onChange={(e) => set(i, { key: e.target.value })} />
          <Select className="v3-select" value={f.operator} aria-label={t("v2.online.filterOperator")}
            onChange={(e) => set(i, { operator: e.target.value as FilterDraft["operator"] })}>
            {OPERATORS.map((op) => <option key={op} value={op}>{op}</option>)}
          </Select>
          <Select className="v3-select" value={f.kind} aria-label={t("v2.online.filterKind")}
            onChange={(e) => {
              const kind = e.target.value as FilterKind;
              set(i, { kind, value: kind === "boolean" ? "true" : "" });
            }}>
            {(["string", "number", "boolean"] as FilterKind[]).map((k) => <option key={k} value={k}>{t(`v2.online.kind.${k}`)}</option>)}
          </Select>
          {f.kind === "boolean" ? (
            <Select className="v3-select" value={f.value} aria-label={t("v2.online.filterValue")} onChange={(e) => set(i, { value: e.target.value })}>
              <option value="true">true</option>
              <option value="false">false</option>
            </Select>
          ) : (
            <input className="v3-input" type={f.kind === "number" ? "number" : "text"} value={f.value}
              aria-label={t("v2.online.filterValue")} onChange={(e) => set(i, { value: e.target.value })} />
          )}
          <Btn size="sm" kind="ghost" title={t("v2.common.delete")} onClick={() => onChange(filters.filter((_, j) => j !== i))}>
            <Trash2 size={13} />
          </Btn>
        </div>
      ))}
      <div>
        <Btn size="sm" disabled={filters.length >= MAX_FILTERS} disabledReason={t("story.reason.maxFilters", { count: MAX_FILTERS })} onClick={() => onChange([...filters, emptyFilter()])}>
          <Plus size={13} /> {t("v2.online.addFilter")}
        </Btn>
      </div>
    </div>
  );
}

/**
 * Create (`id` null) or edit an agent-owned online evaluation config — V2's draft,
 * validation and PATCH. Creating, `agent=` preselects the agent and `seedFrom` copies a
 * config; the run-strategy switch on top leads back to the one-off run wizard.
 */
export function OnlineEditor({ id, seedFrom = null }: { id: string | null; seedFrom?: string | null }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const toast = useToast();
  const { can } = useAuth();
  const { current } = useWorkspace();
  const editing = id !== null;
  const existing = useLoad<OnlineEvalConfigRow | null>(() => (id ? api.v2OnlineConfig(id) : Promise.resolve(null)), `v3-online-edit:${id}`);
  // a copy starts from its source; a source that cannot be read leaves the defaults
  const seed = useLoad(
    () =>
      !editing && seedFrom
        ? api.v2OnlineConfig(seedFrom).then((source) => copyDraft(source, t("v2.tasks.copySuffix")))
        : Promise.resolve(null),
    `v3-online-seed:${editing ? "" : seedFrom}`,
  );
  const agents = useLoad(() => api.listAgents(), `v3-online-agents:${current?.id ?? ""}`);
  const evaluators = useLoad(() => api.v2Evaluators(), `v3-online-evaluators:${current?.id ?? ""}`);
  const [draft, setDraft] = useState<OnlineDraft>(newDraft);
  const [agentId, setAgentId] = useState(() => (editing ? "" : (params.get("agent") ?? "")));
  const [enable, setEnable] = useState(true);
  // a validation string, or the ApiError itself so the notice can show AWS details
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const set = (patch: Partial<OnlineDraft>) => setDraft((prev) => ({ ...prev, ...patch }));

  const row = existing.data;
  useEffect(() => {
    if (row) setDraft(draftFromRow(row));
  }, [row]);
  const copied = seed.data;
  useEffect(() => {
    if (!copied) return;
    setDraft(copied.draft);
    setAgentId(copied.agentId);
  }, [copied]);

  const eligible = useMemo(() => (agents.data?.agents ?? []).filter(eligibleAgent), [agents.data]);
  // a hand-off or copied agent the form cannot watch (inactive, imported) is not picked
  const pickedAgent = eligible.some((a) => a.id === agentId) ? agentId : "";
  const mode: OnlineEvalMode = row ? modeOf(row) : draft.mode;
  const patch = row ? patchBody(row, draft) : null;
  const dirty = patch ? Object.keys(patch).length > 0 : true;
  const back = () => navigate(row ? onlineHref({ view: "detail", id: row.config_id }) : "/v3/online");

  // eval-cost §1.1: a new scores config is priced per day before it is created
  const sampling = Number(draft.sampling);
  const priced = !editing && mode === "scores" && Boolean(pickedAgent) && draft.evaluators.length > 0;
  const estimate = useLoad(
    () => (priced
      ? evalCostApi.estimate({ agent_id: pickedAgent, scope: "online", evaluators: draft.evaluators,
        sampling_percentage: Number.isFinite(sampling) ? Math.min(100, Math.max(0, sampling)) : 10 })
      : Promise.resolve(null)),
    `v3-online-estimate:${priced}:${pickedAgent}:${draft.evaluators.join(",")}:${draft.sampling}`,
  );
  const [confirmCost, setConfirmCost] = useState(false);
  // the estimate a 409 cost refusal carried (the server's figure), until the draft changes
  const [refused, setRefused] = useState<EvalCostEstimate | null>(null);
  const estimateKey = `${priced}:${pickedAgent}:${draft.evaluators.join(",")}:${draft.sampling}`;
  useEffect(() => setRefused(null), [estimateKey]);
  const shown = priced ? refused ?? estimate.data : null;

  // live traffic carries no ground truth: evaluators that need it cannot judge it
  const blocked = (e: EvaluatorRow) => (e.requires_ground_truth || e.id.startsWith("Builtin.Trajectory") ? t("v2.online.needsGt") : null);

  const submit = async () => {
    setError(null);
    if (!editing && !pickedAgent) return setError(t("v2.online.err.agent"));
    const problem = validateDraft(t, draft);
    if (problem) return setError(problem);
    if (shown?.over_limit) return setError(t("v3.evalCost.overLimitHint", { max: shown.max_usd ?? "—" }));
    if (shown?.confirm_required && !confirmCost) return setError(t("v3.evalCost.confirmFirst"));
    setBusy(true);
    try {
      if (row && patch) {
        await api.v2UpdateOnlineConfig(row.config_id, patch);
        toast("ok", t("v2.online.saved"));
        navigate(onlineHref({ view: "detail", id: row.config_id }));
      } else {
        // eval-cost §1.1: a scores config is priced per day and confirmed like a run
        const body = createBody(pickedAgent, draft, enable);
        const created = await api.v2CreateOnlineConfig(mode === "scores" ? { ...body, confirm_cost: confirmCost } : body);
        toast("ok", t("v2.online.created"));
        navigate(onlineHref({ view: "detail", id: created.config_id }));
      }
    } catch (err) {
      const refusal = costRefusal(err);
      if (refusal) {
        setRefused(refusal.estimate);
        if (refusal.kind === "confirm") setConfirmCost(false);
        setError(refusalText(t, refusal));
      } else {
        setError(err);
      }
    } finally {
      setBusy(false);
    }
  };

  if (editing && existing.loading && !row) return <Skeleton rows={6} />;
  if (editing && existing.error && !row) return <Notice s="act"><ApiErrorDetail error={existing.failure ?? existing.error} /></Notice>;
  if (!editing && seedFrom && seed.loading && !copied) return <Skeleton rows={6} />;
  const head = (
    <div>
      <button type="button" className="v3-btn ghost sm" onClick={back}>
        <ArrowLeft size={14} /> {row ? row.name ?? row.config_id : t("v3.online.title")}
      </button>
    </div>
  );
  if (row && !isEditable(row)) {
    return (
      <div className="v3-reveal v3-onl" style={{ display: "grid", gap: 16 }}>
        {head}
        <Notice s="wait">{t("v2.online.notEditable")}</Notice>
      </div>
    );
  }

  return (
    <div className="v3-reveal v3-onl" style={{ display: "grid", gap: 16 }}>
      {head}
      <PageHead
        eyebrow={t("v3.online.eyebrow")}
        title={editing ? t("v2.online.editTitle") : t("v3.online.new")}
        sub={t("v3.online.editorSub")}
        end={
          <Btn kind="primary" busy={busy} disabled={!can("eval.run") || !dirty || (row ? isTransient(row) : false)}
            disabledReason={!can("eval.run") ? permReason(t, "eval.run") : row && isTransient(row) ? t("story.reason.settling") : t("story.reason.noChanges")}
            onClick={() => void submit()}>
            {editing ? t("v3.online.save") : t("v2.online.create")}
          </Btn>
        }
      />
      {!can("eval.run") && <Notice s="wait">{t("v2.tasks.noPermission")}</Notice>}
      {error != null && <Notice s="act"><ApiErrorDetail error={error} /></Notice>}
      {!editing && <StrategySwitch value="continuous" agentId={agents.data ? pickedAgent : agentId} />}

      <Panel title={t("v3.online.what")}>
        <div className="v3-grid c2" style={{ alignItems: "start" }}>
          <label className="v3-field">
            <span>{t("v2.tasks.colAgent")}</span>
            {editing ? (
              <input className="v3-input" value={row?.agent_name ?? row?.agent_id ?? "—"} readOnly />
            ) : (
              <Select className="v3-select" value={pickedAgent} onChange={(e) => setAgentId(e.target.value)}>
                <option value="">{t("v2.common.choose")}</option>
                {eligible.map((a) => <option key={a.id} value={a.id}>{`${a.name} · ${a.method}`}</option>)}
              </Select>
            )}
            <small className="v3-hint">{editing ? t("v2.online.agentFixed") : t("v2.online.agentHint")}</small>
          </label>
          <label className="v3-field">
            <span>{t("v2.tasks.description")}</span>
            <input className="v3-input" maxLength={200} value={draft.description} onChange={(e) => set({ description: e.target.value })} />
            <small className="v3-hint">{t("v2.online.descHint")}</small>
          </label>
        </div>
        <div className="v3-field" style={{ marginTop: 16 }}>
          <span>{t("v2.online.colMode")}</span>
          <div className="v3-onl-modes" role="radiogroup" aria-label={t("v2.online.colMode")}>
            {(["scores", "insights"] as OnlineEvalMode[]).map((m) => (
              <Reasoned key={m} disabledReason={t("v2.online.modeFixed")}>
                <button type="button" role="radio" aria-checked={mode === m} disabled={editing && mode !== m}
                  className={mode === m ? "v3-onl-mode on" : "v3-onl-mode"}
                  onClick={() => !editing && setDraft((prev) => withMode(prev, m))}>
                  <b>{t(`v2.online.mode.${m}`)}</b>
                  <small>{t(`v2.online.modeDesc.${m}`)}</small>
                </button>
              </Reasoned>
            ))}
          </div>
          {editing && <small className="v3-hint">{t("v2.online.modeFixed")}</small>}
        </div>
        {!editing && (
          <label className="v3-onl-check" style={{ marginTop: 14 }}>
            <input type="checkbox" checked={enable} onChange={(e) => setEnable(e.target.checked)} />
            {t("v2.online.enableOnCreate")} — {t("v2.online.enableOnCreateHint")}
          </label>
        )}
      </Panel>

      {mode === "scores" ? (
        <Panel title={t("v2.tasks.pickEvaluators")} end={<span className="mono">{draft.evaluators.length}/{MAX_EVALUATORS}</span>}>
          <p style={{ margin: "0 0 12px", color: "var(--v3-text-3)", fontSize: 13 }}>{t("v2.online.evaluatorsSub", { max: MAX_EVALUATORS })}</p>
          <V2ToastProvider>
            <div className="v2 v3-host">
              <EvaluatorPicker
                evaluators={evaluators.data?.evaluators ?? []}
                loading={evaluators.loading}
                error={evaluators.error}
                onRetry={evaluators.reload}
                selected={draft.evaluators}
                onChange={(next) => set({ evaluators: next })}
                max={MAX_EVALUATORS}
                blockedReason={blocked}
                testIdPrefix="v3-online-eval"
              />
            </div>
          </V2ToastProvider>
        </Panel>
      ) : (
        <Panel title={t("v2.online.insightsTitle")}>
          <p style={{ margin: "0 0 12px", color: "var(--v3-text-3)", fontSize: 13 }}>{t("v2.online.insightsSub")}</p>
          <div className="v3-grid c2" style={{ alignItems: "start" }}>
            <div className="v3-field">
              <span>{t("v2.online.insightTypes")}</span>
              {INSIGHT_TYPES.map((it) => (
                <label key={it} className="v3-onl-check">
                  <input type="checkbox" checked={draft.insights.includes(it)}
                    onChange={() => set({ insights: toggleOrdered(INSIGHT_TYPES, draft.insights as (typeof INSIGHT_TYPES)[number][], it) })} />
                  {insightLabel(t, it)}
                </label>
              ))}
            </div>
            <div className="v3-field">
              <span>{t("v2.online.frequencies")}</span>
              {FREQUENCIES.map((f) => (
                <label key={f} className="v3-onl-check">
                  <input type="checkbox" checked={draft.frequencies.includes(f)} onChange={() => set({ frequencies: toggleOrdered(FREQUENCIES, draft.frequencies, f) })} />
                  {t(`v2.online.freq.${f}`)}
                </label>
              ))}
              <small className="v3-hint">{t("v2.online.frequenciesHint")}</small>
            </div>
          </div>
        </Panel>
      )}

      <Panel title={t("v2.online.samplingTitle")}>
        <div className="v3-grid c2" style={{ alignItems: "start" }}>
          <label className="v3-field">
            <span>{t("v2.tasks.samplingRate")}</span>
            <input className="v3-input" type="number" min={0.01} max={100} step="any" value={draft.sampling}
              onChange={(e) => set({ sampling: e.target.value, samplingTouched: true })} />
            <small className="v3-hint">{t("v2.tasks.samplingHint")}</small>
          </label>
          <label className="v3-field">
            <span>{t("v2.tasks.sessionTimeout")}</span>
            <input className="v3-input" type="number" min={1} max={1440} step={1} value={draft.timeout} onChange={(e) => set({ timeout: e.target.value })} />
            <small className="v3-hint">{t("v2.online.timeoutHint")}</small>
          </label>
        </div>
        <div className="v3-field" style={{ marginTop: 16 }}>
          <span>{t("v2.online.filters")}</span>
          <small className="v3-hint">{t("v2.online.filtersHint", { max: MAX_FILTERS })}</small>
          <FilterRows filters={draft.filters} onChange={(filters) => set({ filters })} />
        </div>
      </Panel>

      {priced && (
        <EstimateCostCard estimate={shown ?? null} loading={estimate.loading} error={estimate.error}
          confirmed={confirmCost} onConfirm={setConfirmCost} />
      )}
    </div>
  );
}
