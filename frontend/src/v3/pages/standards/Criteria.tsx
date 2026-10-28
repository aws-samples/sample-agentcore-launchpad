/**
 * The criteria table (判据): the index of an agent's tables and the editor. The
 * editor makes a bad standard visible before it is signed — red lines on a judge are
 * refused by the server (findings), uncalibrated judges show demoted, the compound
 * rate of all gates sits next to the thresholds, and a version freezes on publish
 * and is signed by someone other than its editor (the server enforces both).
 */
import { Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { ErrorCallout } from "../../../components/ErrorCallout";
import { dlcApi } from "../../../lib/api";
import {
  compoundRate,
  type CriteriaSet,
  type CriteriaSetPayload,
  type Criterion,
  type CriterionInput,
  DLC_DIMENSIONS,
  DLC_TIERS,
  type DlcDimension,
  type DlcTier,
  editorRows,
  type EditorRow,
  type ExecutorKind,
  refusedFindings,
} from "../../../lib/dlc";
import { useLoad, useToast } from "../../hooks";
import type { NextStepNote } from "../../story/flow";
import { useNextStep } from "../../story/useNextStep";
import { StoryPrereqs } from "../../story/Prereqs";
import { Btn, Chip, Confirm, Dialog, Empty, Lamp, Notice, Panel, Skeleton, Stat } from "../../ui";
import { TierChip } from "./charts";
import { findingText, stamp } from "./common";
import { Select } from "../../select";

const METRICS = ["latency_p95_ms", "cost_per_success_usd", "tokens_per_session"] as const;

function toInput(row: Criterion): CriterionInput {
  return {
    key: row.key,
    text: row.text,
    dimension: row.dimension,
    tier: row.tier,
    threshold: row.threshold,
    metric_rule: row.metric_rule,
    level: row.level,
    executor: row.executor,
    denominator: row.denominator,
    expected_type: row.expected_type,
    pass_k: row.pass_k,
    attribution_layer: row.attribution_layer,
    owner: row.owner,
    examples: row.examples,
    notes: row.notes,
  };
}

function blank(index: number): CriterionInput {
  return {
    key: `C${index + 1}`,
    text: "",
    dimension: "quality",
    tier: "gate",
    threshold: 0.9,
    level: "session",
    executor: { kind: "evaluator" },
    denominator: "sessions",
    expected_type: "deterministic",
    notes: "",
    examples: [],
  };
}

/* ── index ───────────────────────────────────────────────────────────────── */

export function CriteriaIndex({
  agentId,
  sets,
  loading,
  error,
  onReload,
  onOpen,
}: {
  agentId: string | null;
  sets: CriteriaSet[];
  loading: boolean;
  error: string | null;
  onReload: () => void;
  onOpen: (lineageId: string) => void;
}) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useToast();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [template, setTemplate] = useState("");
  const [busy, setBusy] = useState(false);
  const templates = useLoad(() => dlcApi.listSets({ kind: "template" }), "v3-std-templates");
  const [failure, setFailure] = useState<unknown>(null);

  const create = (kind: "agent" | "template") => {
    setBusy(true);
    setFailure(null);
    const chosen = templates.data?.sets.find((s) => s.lineage_id === template);
    dlcApi
      .createSet({
        kind,
        name,
        agent_id: kind === "agent" ? agentId : null,
        template_lineage_id: kind === "agent" && template ? template : null,
        template_version: kind === "agent" && chosen ? chosen.version : null,
      })
      .then((created) => {
        toast("ok", t("v2.dlc.criteria.created"));
        setCreating(false);
        onReload();
        onOpen(created.set.lineage_id);
      })
      .catch((err: unknown) => setFailure(err))
      .finally(() => setBusy(false));
  };

  return (
    <>
      <Panel
        title={t("v2.dlc.criteria.indexTitle")}
        flush
        end={
          can("criteria.manage") ? (
            <Btn size="sm" kind="primary" disabled={!agentId} disabledReason={t("v2.dlc.pickAgentFirst")}
              onClick={() => {
                setName("");
                setTemplate("");
                setCreating(true);
              }}>
              <Plus size={13} /> {t("v2.dlc.criteria.create")}
            </Btn>
          ) : undefined
        }
      >
        {loading && sets.length === 0 ? (
          <div style={{ padding: 20 }}><Skeleton rows={3} /></div>
        ) : error ? (
          <div style={{ padding: 20 }}><Notice s="act">{error}</Notice> <Btn size="sm" onClick={onReload}>{t("v3.standards.retry")}</Btn></div>
        ) : sets.length === 0 ? (
          <Empty title={t("v2.dlc.criteria.noSets")}>
            {t("v2.dlc.criteria.indexSub")}
            <StoryPrereqs agentId={agentId} stepKey="evaluate.criteria.draft" />
          </Empty>
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v3.standards.name")}</th>
                <th>{t("v2.dlc.criteria.kind")}</th>
                <th>{t("v2.dlc.criteria.version")}</th>
                <th>{t("v3.standards.status")}</th>
                <th>{t("v2.dlc.criteria.signedByCol")}</th>
              </tr>
            </thead>
            <tbody>
              {sets.map((row) => {
                const s = row.status !== "published" ? "info" : row.signed_by ? "ok" : "wait";
                return (
                  <tr key={`${row.lineage_id}:${row.version}`} className="click" onClick={() => onOpen(row.lineage_id)}>
                    <td style={{ width: 30 }}><Lamp s={s} /></td>
                    <td><b>{row.name}</b></td>
                    <td>{t(`v2.dlc.criteria.kindOf.${row.kind}`)}</td>
                    <td className="mono">v{row.version}</td>
                    <td>
                      <Chip s={row.status === "published" ? "ok" : "info"}>{t(`v2.dlc.criteria.status.${row.status}`)}</Chip>{" "}
                      {row.status === "published" && !row.signed_by && <Chip s="wait">{t("v2.dlc.criteria.unsigned")}</Chip>}
                    </td>
                    <td>{row.signed_by ?? "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>

      {creating && (
        <Dialog
          title={t("v2.dlc.criteria.createTitle")}
          onClose={() => { setCreating(false); setFailure(null); }}
          foot={
            <>
              <Btn kind="ghost" onClick={() => setCreating(false)}>{t("v3.common.cancel")}</Btn>
              <Btn busy={busy} disabled={name.trim() === ""} disabledReason={t("story.reason.fillName")} onClick={() => create("template")}>{t("v2.dlc.criteria.createTemplate")}</Btn>
              <Btn kind="primary" busy={busy} disabled={name.trim() === ""} disabledReason={t("story.reason.fillName")} onClick={() => create("agent")}>{t("v2.dlc.criteria.createForAgent")}</Btn>
            </>
          }
        >
          <div style={{ display: "grid", gap: 12 }}>
            <p style={{ margin: 0 }}>{t("v2.dlc.criteria.createHint")}</p>
            <label className="v3-field">
              <span>{t("v3.standards.name")}</span>
              <input className="v3-input" value={name} maxLength={96} onChange={(e) => setName(e.target.value)} />
            </label>
            <label className="v3-field">
              <span>{t("v2.dlc.criteria.fromTemplate")}</span>
              <Select className="v3-select" value={template} onChange={(e) => setTemplate(e.target.value)}>
                <option value="">{t("v2.dlc.criteria.noTemplate")}</option>
                {(templates.data?.sets ?? [])
                  .filter((s) => s.status === "published")
                  .map((s) => (
                    <option key={s.lineage_id} value={s.lineage_id}>{`${s.name} v${s.version}`}</option>
                  ))}
              </Select>
              <small className="v3-hint">{t("v2.dlc.criteria.fromTemplateHint")}</small>
            </label>
            <ErrorCallout error={failure} params={{ agent_id: agentId ?? undefined }} onDismiss={() => setFailure(null)}
              data-testid="v3-criteria-create-error" />
          </div>
        </Dialog>
      )}
    </>
  );
}

/* ── editor ──────────────────────────────────────────────────────────────── */

function RowEditor({ row, onChange, onRemove }: { row: CriterionInput; onChange: (next: CriterionInput) => void; onRemove: () => void }) {
  const { t } = useTranslation();
  const kind = row.executor?.kind ?? "evaluator";
  const set = (patch: Partial<CriterionInput>) => onChange({ ...row, ...patch });
  return (
    <div className="v3-std-row">
      <div className="grid4">
        <label className="v3-field">
          <span>{t("v2.dlc.criteria.key")}</span>
          <input className="v3-input mono" value={row.key} maxLength={32} onChange={(e) => set({ key: e.target.value })} />
        </label>
        <label className="v3-field">
          <span>{t("v2.dlc.criteria.dimension")}</span>
          <Select className="v3-select" value={row.dimension} onChange={(e) => set({ dimension: e.target.value as DlcDimension })}>
            {DLC_DIMENSIONS.map((d) => <option key={d} value={d}>{t(`v2.dlc.dimension.${d}`)}</option>)}
          </Select>
        </label>
        <label className="v3-field">
          <span>{t("v2.dlc.criteria.tier")}</span>
          <Select className="v3-select" value={row.tier} onChange={(e) => set({ tier: e.target.value as DlcTier })}>
            {DLC_TIERS.map((d) => <option key={d} value={d}>{t(`v2.dlc.tier.${d}`)}</option>)}
          </Select>
        </label>
        <label className="v3-field">
          <span>{t("v2.dlc.criteria.executor")}</span>
          <Select className="v3-select" value={kind}
            onChange={(e) => {
              const v = e.target.value;
              set({
                executor: { ...row.executor, kind: v as ExecutorKind },
                metric_rule: v === "metric" ? row.metric_rule ?? { metric: "latency_p95_ms", op: "<=", value: 3000 } : null,
              });
            }}>
            <option value="evaluator">{t("v2.dlc.criteria.executorEvaluator")}</option>
            <option value="metric">{t("v2.dlc.criteria.executorMetric")}</option>
            <option value="human">{t("v2.dlc.criteria.executorManual")}</option>
          </Select>
        </label>
      </div>
      <label className="v3-field">
        <span>{t("v2.dlc.criteria.text")}</span>
        <textarea className="v3-input" rows={2} value={row.text} maxLength={2000} onChange={(e) => set({ text: e.target.value })} />
        <small className="v3-hint">{t("v2.dlc.criteria.textHint")}</small>
      </label>
      <div className="grid4">
        {kind === "evaluator" && (
          <label className="v3-field">
            <span>{t("v2.dlc.criteria.evaluatorId")}</span>
            <input className="v3-input mono" value={row.executor?.evaluator_id ?? ""}
              onChange={(e) => set({ executor: { ...row.executor, evaluator_id: e.target.value } })} />
          </label>
        )}
        {kind === "metric" ? (
          <>
            <label className="v3-field">
              <span>{t("v2.dlc.criteria.metric")}</span>
              <Select className="v3-select" value={row.metric_rule?.metric ?? "latency_p95_ms"}
                onChange={(e) => set({ metric_rule: { ...row.metric_rule, metric: e.target.value } })}>
                {METRICS.map((m) => <option key={m} value={m}>{t(`v2.dlc.metric.${m}`)}</option>)}
              </Select>
            </label>
            <label className="v3-field">
              <span>{t("v2.dlc.criteria.bound")}</span>
              <input className="v3-input" type="number" value={row.metric_rule?.value ?? 0}
                onChange={(e) => set({ metric_rule: { ...row.metric_rule, value: Number(e.target.value) } })} />
            </label>
          </>
        ) : (
          <label className="v3-field">
            <span>{t("v2.dlc.criteria.threshold")}</span>
            <input className="v3-input" type="number" min={0} max={100} step={1}
              value={row.threshold === null || row.threshold === undefined ? "" : Math.round(row.threshold * 100)}
              onChange={(e) => set({ threshold: e.target.value === "" ? null : Number(e.target.value) / 100 })} />
            <small className="v3-hint">{t("v2.dlc.criteria.thresholdHint")}</small>
          </label>
        )}
        <label className="v3-field">
          <span>{t("v2.dlc.criteria.denominator")}</span>
          <Select className="v3-select" value={row.denominator ?? "sessions"}
            onChange={(e) => set({ denominator: e.target.value as "sessions" | "turns" | "fields" })}>
            {["sessions", "turns", "fields"].map((d) => <option key={d} value={d}>{t(`v2.dlc.denominator.${d}`)}</option>)}
          </Select>
        </label>
        <label className="v3-field">
          <span>{t("v2.dlc.criteria.passK")}</span>
          <input className="v3-input" type="number" min={1} max={10} value={row.pass_k?.k ?? 1}
            onChange={(e) => {
              const k = Number(e.target.value);
              set({ pass_k: k > 1 ? { k, mode: row.pass_k?.mode ?? "all" } : null });
            }} />
          <small className="v3-hint">{t("v2.dlc.criteria.passKHint")}</small>
        </label>
      </div>
      <label className="v3-field">
        <span>{t("v2.dlc.criteria.notes")}</span>
        <input className="v3-input" value={row.notes ?? ""} onChange={(e) => set({ notes: e.target.value })} />
      </label>
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <Btn size="sm" kind="ghost" onClick={onRemove}><Trash2 size={13} /> {t("v3.standards.removeRow")}</Btn>
      </div>
    </div>
  );
}

export function CriteriaEditor({ payload, onReload, onBack }: { payload: CriteriaSetPayload; onReload: () => void; onBack: () => void }) {
  const { t } = useTranslation();
  const { can, username } = useAuth();
  const toast = useToast();
  const [entries, setEntries] = useState<EditorRow[]>(() => editorRows(payload.criteria.map(toInput)));
  const rows = entries.map((e) => e.row);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [signing, setSigning] = useState(false);
  const [signNote, setSignNote] = useState("");
  const [confirmPublish, setConfirmPublish] = useState(false);
  const [failure, setFailure] = useState<unknown>(null);
  const set = payload.set;
  const frozen = set.status === "published";
  const errors = payload.findings.filter((f) => f.level === "error");
  // a refused save / publish names what is wrong with the draft as typed, not as saved
  const refused = refusedFindings(failure);
  const shownFindings = refused ?? payload.findings;
  const mayEdit = can("criteria.manage");
  const maySign = can("criteria.sign");

  // recomputed locally so the number moves as the thresholds are typed
  const gates = rows.filter((r) => r.tier === "gate" && (r.executor?.kind ?? "evaluator") !== "metric" && typeof r.threshold === "number");
  const compound = compoundRate(gates.map((r) => r.threshold as number));
  const fractional = rows.filter(
    (r) => typeof r.threshold === "number" && Math.abs((r.threshold as number) * 100 - Math.round((r.threshold as number) * 100)) > 1e-9,
  );

  // publish / sign / save fail in place, with the way out (storyline-ux §4); a
  // published or signed agent table says what comes next instead (§3.1)
  const nextStep = useNextStep();
  const storyNote = (doneKey: string, after: string): NextStepNote | undefined =>
    set.kind === "agent" && set.agent_id
      ? { done: t(doneKey, { version: set.version }), after, agentId: set.agent_id, params: { lineage_id: set.lineage_id } }
      : undefined;
  const run = async (fn: () => Promise<unknown>, okKey: string, note?: NextStepNote) => {
    setBusy(true);
    setFailure(null);
    nextStep.clear();
    try {
      await fn();
      if (note) nextStep.show(note);
      else toast("ok", t(okKey));
      setDirty(false);
      onReload();
    } catch (err) {
      setFailure(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div>
        <button type="button" className="v3-btn ghost sm" onClick={onBack}>← {t("v2.dlc.criteria.backToList")}</button>
      </div>
      <ErrorCallout error={failure} params={{ agent_id: set.agent_id ?? undefined, lineage_id: set.lineage_id }}
        onDismiss={() => setFailure(null)} data-testid="v3-criteria-error" />
      {nextStep.card}
      <Panel
        title={t("v2.dlc.criteria.tableTitle", { name: set.name, version: set.version })}
        signal={!frozen ? "info" : set.signed_by ? "ok" : "wait"}
        end={
          <span style={{ display: "inline-flex", gap: 6 }}>
            <Chip s={frozen ? "ok" : "info"}>{t(`v2.dlc.criteria.status.${set.status}`)}</Chip>
            {set.signed_by ? <Chip s="ok">{t("v2.dlc.criteria.signedBy", { who: set.signed_by })}</Chip> : <Chip s="wait">{t("v2.dlc.criteria.unsigned")}</Chip>}
          </span>
        }
      >
        <div className="v3-grid c4">
          <Stat label={t("v2.dlc.criteria.count")} value={payload.summary.count} />
          <Stat label={t("v2.dlc.criteria.redlines")} value={payload.summary.tiers.redline ?? 0} />
          <Stat label={t("v2.dlc.criteria.effectiveGates")} value={`${payload.summary.effective_gates} / ${payload.summary.declared_gates}`}
            signal={payload.summary.effective_gates < payload.summary.declared_gates ? "wait" : undefined} />
          <Stat label={t("v2.dlc.criteria.judgeShare")} value={`${Math.round(payload.summary.judge_share * 100)}%`}
            foot={t(`v2.dlc.cadence.${payload.summary.run_cadence}`)} />
        </div>
        <div style={{ display: "grid", gap: 8, marginTop: 14 }}>
          {payload.summary.effective_gates < payload.summary.declared_gates && (
            <Notice s="wait">{t("v2.dlc.criteria.demotedGates", { n: payload.summary.declared_gates - payload.summary.effective_gates })}</Notice>
          )}
          {payload.newer_template_version !== null && (
            <Notice>
              {t("v2.dlc.criteria.templateNewer", { v: payload.newer_template_version })}{" "}
              {mayEdit && (
                <Btn size="sm" disabled={busy}
                  onClick={() => void run(() => dlcApi.adoptTemplate(set.lineage_id, payload.newer_template_version as number), "v2.dlc.criteria.adopted")}>
                  {t("v2.dlc.criteria.adopt")}
                </Btn>
              )}
            </Notice>
          )}
          {refused && <Notice s="act">{t("v2.dlc.criteria.refusedFindings")}</Notice>}
          {[...shownFindings.filter((f) => f.level === "error"), ...shownFindings.filter((f) => f.level === "warning")].map((f, i) => (
            <Notice key={`${f.code}:${f.key ?? ""}:${i}`} s={f.level === "error" ? "act" : "wait"}>
              {f.key && <span className="mono">{f.key} · </span>}
              {findingText(t, f)}
            </Notice>
          ))}
        </div>
      </Panel>

      <Panel title={t("v2.dlc.criteria.assistant")} signal={compound !== null && compound < 0.8 ? "wait" : undefined}>
        <div className="v3-grid c2">
          <Stat label={t("v2.dlc.criteria.gateCount")} value={gates.length} />
          <Stat label={t("v2.dlc.criteria.compound")} value={compound === null ? "—" : `${(compound * 100).toFixed(1)}%`}
            signal={compound !== null && compound < 0.8 ? "wait" : undefined} foot={t("v2.dlc.criteria.assistantSub")} />
        </div>
        {compound !== null && compound < 0.8 && (
          <div style={{ marginTop: 12 }}><Notice s="wait">{t("v2.dlc.criteria.compoundWarn", { v: `${(compound * 100).toFixed(1)}%`, n: gates.length })}</Notice></div>
        )}
        {fractional.length > 0 && (
          <div style={{ marginTop: 8 }}><Notice s="wait">{t("v2.dlc.criteria.integerWarn", { keys: fractional.map((r) => r.key).join(", ") })}</Notice></div>
        )}
      </Panel>

      <Panel
        title={t("v2.dlc.criteria.rows")}
        flush={frozen}
        end={
          mayEdit && !frozen ? (
            <Btn size="sm" onClick={() => { setEntries((prev) => [...prev, ...editorRows([blank(prev.length)])]); setDirty(true); }}>
              <Plus size={13} /> {t("v2.dlc.criteria.addRow")}
            </Btn>
          ) : undefined
        }
      >
        {frozen ? (
          <table className="v3-table">
            <thead>
              <tr>
                <th>{t("v2.dlc.criteria.key")}</th>
                <th>{t("v2.dlc.criteria.text")}</th>
                <th>{t("v2.dlc.criteria.dimension")}</th>
                <th>{t("v2.dlc.criteria.tier")}</th>
                <th className="num">{t("v2.dlc.criteria.threshold")}</th>
                <th>{t("v2.dlc.criteria.executor")}</th>
              </tr>
            </thead>
            <tbody>
              {payload.criteria.map((row) => (
                <tr key={row.key}>
                  <td className="mono">{row.key}</td>
                  <td>{row.text}</td>
                  <td>{t(`v2.dlc.dimension.${row.dimension}`)}</td>
                  <td><TierChip tier={row.tier} effective={row.effective_tier} /></td>
                  <td className="num">
                    {row.executor.kind === "metric"
                      ? `${row.metric_rule?.op ?? "<="} ${row.metric_rule?.value ?? "—"}`
                      : row.threshold === null ? "—" : `${Math.round(row.threshold * 100)}%`}
                  </td>
                  <td className="mono">{row.executor.evaluator_id ?? row.executor.kind ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : rows.length === 0 ? (
          <p className="v3-std-empty">{t("v2.dlc.criteria.noRows")}</p>
        ) : (
          <div style={{ display: "grid", gap: 12 }}>
            {entries.map(({ uid, row }) => (
              <RowEditor key={uid} row={row}
                onChange={(next) => { setEntries((prev) => prev.map((e) => (e.uid === uid ? { uid, row: next } : e))); setDirty(true); }}
                onRemove={() => { setEntries((prev) => prev.filter((e) => e.uid !== uid)); setDirty(true); }} />
            ))}
          </div>
        )}
      </Panel>

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
        {!frozen && mayEdit && (
          <>
            <Btn kind="primary" busy={busy} disabled={!dirty} disabledReason={t("story.reason.noChanges")}
              onClick={() => void run(() => dlcApi.saveSet(set.lineage_id, { criteria: rows }), "v2.dlc.criteria.saved")}>
              {t("v3.standards.save")}
            </Btn>
            <Btn busy={busy} disabled={dirty || errors.length > 0 || rows.length === 0}
              disabledReason={errors.length > 0 ? t("v2.dlc.criteria.fixFirst") : dirty ? t("v3.standards.saveFirst") : t("story.reason.addCriterion")}
              onClick={() => setConfirmPublish(true)}>
              {t("v2.dlc.criteria.publish")}
            </Btn>
          </>
        )}
        {frozen && mayEdit && (
          <Btn disabled={busy} onClick={() => void run(() => dlcApi.newSetVersion(set.lineage_id), "v2.dlc.criteria.newVersion")}>
            {t("v2.dlc.criteria.openNewVersion")}
          </Btn>
        )}
        {frozen && !set.signed_by && maySign && (
          <Btn kind="primary" disabled={busy} onClick={() => { setSignNote(""); setSigning(true); }}>{t("v2.dlc.criteria.sign")}</Btn>
        )}
        {frozen && !set.signed_by && !maySign && <span className="v3-std-muted">{t("v2.dlc.criteria.needsSigner")}</span>}
        {set.signed_by === username && <span className="v3-std-muted">{t("v2.dlc.criteria.youSigned")}</span>}
      </div>

      <Panel title={t("v2.dlc.criteria.versions")} flush>
        <table className="v3-table">
          <thead>
            <tr>
              <th>{t("v2.dlc.criteria.version")}</th>
              <th>{t("v3.standards.status")}</th>
              <th>{t("v2.dlc.criteria.signedByCol")}</th>
              <th>{t("v2.dlc.criteria.publishedAt")}</th>
            </tr>
          </thead>
          <tbody>
            {payload.versions.map((v) => (
              <tr key={v.version} style={v.version === set.version ? { background: "var(--v3-ink-3)" } : undefined}>
                <td className="mono">v{v.version}</td>
                <td>{t(`v2.dlc.criteria.status.${v.status}`)}</td>
                <td>{v.signed_by ?? "—"}</td>
                <td>{stamp(v.published_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      {confirmPublish && (
        <Confirm title={t("v2.dlc.criteria.publishTitle")} confirmLabel={t("v2.dlc.criteria.publish")} cancelLabel={t("v3.common.cancel")}
          busy={busy} onCancel={() => setConfirmPublish(false)}
          onConfirm={() => { setConfirmPublish(false); void run(() => dlcApi.publishSet(set.lineage_id), "v2.dlc.criteria.published",
            storyNote("story.done.criteriaPublished", "evaluate.criteria.draft")); }}>
          {t("v2.dlc.criteria.publishText", { version: set.version })}
        </Confirm>
      )}

      {signing && (
        <Dialog
          wide
          title={t("v2.dlc.criteria.signTitle")}
          onClose={() => setSigning(false)}
          foot={
            <>
              <Btn kind="ghost" onClick={() => setSigning(false)}>{t("v3.common.cancel")}</Btn>
              <Btn kind="primary" disabled={busy}
                onClick={() => { setSigning(false); void run(() => dlcApi.signSet(set.lineage_id, signNote), "v2.dlc.criteria.signed",
                  storyNote("story.done.criteriaSigned", "evaluate.criteria.sign")); }}>
                {t("v2.dlc.criteria.sign")}
              </Btn>
            </>
          }
        >
          <div style={{ display: "grid", gap: 12 }}>
            <p style={{ margin: 0 }}>{t("v2.dlc.criteria.signHint")}</p>
            <ul className="v3-std-signlist">
              {payload.criteria.filter((c) => c.tier !== "observe").map((c) => (
                <li key={c.key}>
                  <span className="mono">{c.key}</span>
                  <TierChip tier={c.tier} effective={c.effective_tier} />
                  <span>{c.text}</span>
                </li>
              ))}
            </ul>
            <label className="v3-field">
              <span>{t("v2.dlc.criteria.signNote")}</span>
              <textarea className="v3-input" rows={3} value={signNote} onChange={(e) => setSignNote(e.target.value)} />
            </label>
          </div>
        </Dialog>
      )}
    </div>
  );
}
