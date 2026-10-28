/**
 * The criteria table (判据) editor — §7.1 and §7.2 of the design.
 *
 * The thing a person decides here is *what good means*, so the editor's job is to
 * make a bad standard visible before it is signed:
 *
 * - a red line on an LLM judge is refused, not warned about;
 * - a judge criterion is shown demoted to `observe` until it is calibrated, so the
 *   table never claims to gate on something that cannot gate;
 * - the compound rate of all gates is shown next to the thresholds, because eight
 *   independent 95% gates let only 66% of sessions through and nobody types that
 *   number intending it;
 * - a version is frozen on publish and signed by someone other than its editor.
 */
import { Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { dlcApi, errorMessage } from "../../../lib/api";
import {
  DLC_DIMENSIONS,
  DLC_TIERS,
  type CriteriaFinding,
  type CriteriaSetPayload,
  type Criterion,
  type CriterionInput,
  type DlcDimension,
  type DlcTier,
  type EditorRow,
  type ExecutorKind,
  compoundRate,
  editorRows,
  refusedFindings,
} from "../../../lib/dlc";
import { useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  Confirm,
  Field,
  Kpi,
  LinkButton,
  Modal,
  Select,
  Tag,
} from "../../ui";
import { TierChip } from "./charts";

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

function Findings({ findings }: { findings: CriteriaFinding[] }) {
  const { t } = useTranslation();
  if (findings.length === 0) return null;
  const errors = findings.filter((f) => f.level === "error");
  return (
    <div className="v2-dlc-findings">
      {[...errors, ...findings.filter((f) => f.level === "warning")].map((f, i) => (
        <div key={`${f.code}:${f.key ?? ""}:${i}`} className={`row ${f.level}`}>
          <Tag tone={f.level === "error" ? "red" : "orange"}>
            {f.key ?? t(`v2.dlc.criteria.findingLevel.${f.level}`)}
          </Tag>
          <span>{f.message}</span>
        </div>
      ))}
    </div>
  );
}

/**
 * Threshold assistant: the gates a person just typed, and what they mean together.
 * `compound_gate_rate` comes from the server for the saved table; while editing we
 * recompute locally so the number moves as the thresholds are typed.
 */
function ThresholdAssistant({ rows }: { rows: CriterionInput[] }) {
  const { t } = useTranslation();
  const gates = rows.filter(
    (r) => r.tier === "gate" && (r.executor?.kind ?? "evaluator") !== "metric" && typeof r.threshold === "number",
  );
  const compound = compoundRate(gates.map((r) => r.threshold as number));
  const integer = rows.filter(
    (r) => typeof r.threshold === "number" && Math.abs((r.threshold as number) * 100 - Math.round((r.threshold as number) * 100)) > 1e-9,
  );
  return (
    <Card title={t("v2.dlc.criteria.assistant")} sub={t("v2.dlc.criteria.assistantSub")}>
      <div className="v2-dlc-kpis">
        <Kpi label={t("v2.dlc.criteria.gateCount")} value={String(gates.length)} />
        <Kpi
          label={t("v2.dlc.criteria.compound")}
          value={compound === null ? "—" : `${(compound * 100).toFixed(1)}%`}
        />
      </div>
      {compound !== null && compound < 0.8 && (
        <Alert tone="warn">{t("v2.dlc.criteria.compoundWarn", { v: `${(compound * 100).toFixed(1)}%`, n: gates.length })}</Alert>
      )}
      {integer.length > 0 && (
        <Alert tone="warn">
          {t("v2.dlc.criteria.integerWarn", { keys: integer.map((r) => r.key).join(", ") })}
        </Alert>
      )}
    </Card>
  );
}

function RowEditor({
  row,
  onChange,
  onRemove,
}: {
  row: CriterionInput;
  onChange: (next: CriterionInput) => void;
  onRemove: () => void;
}) {
  const { t } = useTranslation();
  const kind = row.executor?.kind ?? "evaluator";
  const set = (patch: Partial<CriterionInput>) => onChange({ ...row, ...patch });
  return (
    <div className="v2-dlc-row">
      <div className="grid">
        <Field label={t("v2.dlc.criteria.key")}>
          <input
            className="v2-input mono"
            value={row.key}
            maxLength={32}
            onChange={(e) => set({ key: e.target.value })}
          />
        </Field>
        <Field label={t("v2.dlc.criteria.dimension")}>
          <Select
            value={row.dimension}
            onChange={(v) => set({ dimension: v as DlcDimension })}
            options={DLC_DIMENSIONS.map((d) => ({ value: d, label: t(`v2.dlc.dimension.${d}`) }))}
          />
        </Field>
        <Field label={t("v2.dlc.criteria.tier")}>
          <Select
            value={row.tier}
            onChange={(v) => set({ tier: v as DlcTier })}
            options={DLC_TIERS.map((d) => ({ value: d, label: t(`v2.dlc.tier.${d}`) }))}
          />
        </Field>
        <Field label={t("v2.dlc.criteria.executor")}>
          <Select
            value={kind}
            onChange={(v) =>
              set({
                executor: { ...row.executor, kind: v as ExecutorKind },
                metric_rule: v === "metric" ? row.metric_rule ?? { metric: "latency_p95_ms", op: "<=", value: 3000 } : null,
              })
            }
            options={[
              { value: "evaluator", label: t("v2.dlc.criteria.executorEvaluator") },
              { value: "metric", label: t("v2.dlc.criteria.executorMetric") },
              { value: "human", label: t("v2.dlc.criteria.executorManual") },
            ]}
          />
        </Field>
      </div>
      <Field label={t("v2.dlc.criteria.text")} hint={t("v2.dlc.criteria.textHint")}>
        <textarea
          className="v2-input"
          rows={2}
          value={row.text}
          maxLength={2000}
          onChange={(e) => set({ text: e.target.value })}
        />
      </Field>
      <div className="grid">
        {kind === "evaluator" && (
          <Field label={t("v2.dlc.criteria.evaluatorId")}>
            <input
              className="v2-input mono"
              value={row.executor?.evaluator_id ?? ""}
              onChange={(e) => set({ executor: { ...row.executor, evaluator_id: e.target.value } })}
            />
          </Field>
        )}
        {kind === "metric" ? (
          <>
            <Field label={t("v2.dlc.criteria.metric")}>
              <Select
                value={row.metric_rule?.metric ?? "latency_p95_ms"}
                onChange={(v) => set({ metric_rule: { ...row.metric_rule, metric: v } })}
                options={METRICS.map((m) => ({ value: m, label: t(`v2.dlc.metric.${m}`) }))}
              />
            </Field>
            <Field label={t("v2.dlc.criteria.bound")}>
              <input
                className="v2-input"
                type="number"
                value={row.metric_rule?.value ?? 0}
                onChange={(e) => set({ metric_rule: { ...row.metric_rule, value: Number(e.target.value) } })}
              />
            </Field>
          </>
        ) : (
          <Field label={t("v2.dlc.criteria.threshold")} hint={t("v2.dlc.criteria.thresholdHint")}>
            <input
              className="v2-input"
              type="number"
              min={0}
              max={100}
              step={1}
              value={row.threshold === null || row.threshold === undefined ? "" : Math.round(row.threshold * 100)}
              onChange={(e) =>
                set({ threshold: e.target.value === "" ? null : Number(e.target.value) / 100 })
              }
            />
          </Field>
        )}
        <Field label={t("v2.dlc.criteria.denominator")}>
          <Select
            value={row.denominator ?? "sessions"}
            onChange={(v) => set({ denominator: v as "sessions" | "turns" | "fields" })}
            options={["sessions", "turns", "fields"].map((d) => ({ value: d, label: t(`v2.dlc.denominator.${d}`) }))}
          />
        </Field>
        <Field label={t("v2.dlc.criteria.passK")} hint={t("v2.dlc.criteria.passKHint")}>
          <input
            className="v2-input"
            type="number"
            min={1}
            max={10}
            value={row.pass_k?.k ?? 1}
            onChange={(e) => {
              const k = Number(e.target.value);
              set({ pass_k: k > 1 ? { k, mode: row.pass_k?.mode ?? "all" } : null });
            }}
          />
        </Field>
      </div>
      <Field label={t("v2.dlc.criteria.notes")} hint={t("v2.dlc.criteria.notesHint")}>
        <input
          className="v2-input"
          value={row.notes ?? ""}
          onChange={(e) => set({ notes: e.target.value })}
        />
      </Field>
      <div className="v2-dlc-rowfoot">
        <LinkButton danger onClick={onRemove}>
          <Trash2 size={13} /> {t("v2.common.delete")}
        </LinkButton>
      </div>
    </div>
  );
}

export function CriteriaEditor({
  payload,
  onReload,
}: {
  payload: CriteriaSetPayload;
  onReload: () => void;
}) {
  const { t } = useTranslation();
  const { can, username } = useAuth();
  const toast = useV2Toast();
  const [entries, setEntries] = useState<EditorRow[]>(() => editorRows(payload.criteria.map(toInput)));
  const rows = entries.map((e) => e.row);
  // a refused save / publish names what is wrong with the draft as typed, not as saved
  const [refused, setRefused] = useState<CriteriaFinding[] | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [signing, setSigning] = useState(false);
  const [signNote, setSignNote] = useState("");
  const [confirmPublish, setConfirmPublish] = useState(false);
  const set = payload.set;
  const frozen = set.status === "published";
  const errors = payload.findings.filter((f) => f.level === "error");
  const mayEdit = can("criteria.manage");
  const maySign = can("criteria.sign");
  const signedBySelf = set.signed_by === username;

  const byKey = useMemo(() => new Map(payload.criteria.map((c) => [c.key, c])), [payload.criteria]);

  const update = (uid: number, next: CriterionInput) => {
    setEntries((prev) => prev.map((e) => (e.uid === uid ? { uid, row: next } : e)));
    setDirty(true);
  };

  const run = async (fn: () => Promise<unknown>, okKey: string) => {
    setBusy(true);
    try {
      await fn();
      toast("success", t(okKey));
      setRefused(null);
      setDirty(false);
      onReload();
    } catch (error) {
      setRefused(refusedFindings(error));
      toast("error", errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Card
        title={t("v2.dlc.criteria.tableTitle", { name: set.name, version: set.version })}
        sub={t(`v2.dlc.criteria.status.${set.status}`)}
        end={
          <>
            {set.signed_by ? (
              <Tag tone="green">{t("v2.dlc.criteria.signedBy", { who: set.signed_by })}</Tag>
            ) : (
              <Tag tone="orange">{t("v2.dlc.criteria.unsigned")}</Tag>
            )}
            {frozen && <Tag tone="gray">{t("v2.dlc.criteria.frozen")}</Tag>}
          </>
        }
      >
        <div className="v2-dlc-kpis">
          <Kpi label={t("v2.dlc.criteria.count")} value={String(payload.summary.count)} />
          <Kpi label={t("v2.dlc.criteria.redlines")} value={String(payload.summary.tiers.redline ?? 0)} />
          <Kpi
            label={t("v2.dlc.criteria.effectiveGates")}
            value={`${payload.summary.effective_gates} / ${payload.summary.declared_gates}`}
          />
          <Kpi
            label={t("v2.dlc.criteria.judgeShare")}
            value={`${Math.round(payload.summary.judge_share * 100)}%`}
          />
          <Kpi label={t("v2.dlc.criteria.cadence")} value={t(`v2.dlc.cadence.${payload.summary.run_cadence}`)} />
        </div>
        {payload.summary.effective_gates < payload.summary.declared_gates && (
          <Alert tone="warn">
            {t("v2.dlc.criteria.demotedGates", {
              n: payload.summary.declared_gates - payload.summary.effective_gates,
            })}
          </Alert>
        )}
        {payload.newer_template_version !== null && (
          <Alert
            action={
              mayEdit ? (
                <LinkButton
                  onClick={() =>
                    void run(
                      () => dlcApi.adoptTemplate(set.lineage_id, payload.newer_template_version as number),
                      "v2.dlc.criteria.adopted",
                    )
                  }
                >
                  {t("v2.dlc.criteria.adopt")}
                </LinkButton>
              ) : undefined
            }
          >
            {t("v2.dlc.criteria.templateNewer", { v: payload.newer_template_version })}
          </Alert>
        )}
        {refused && <Alert tone="error">{t("v2.dlc.criteria.refusedFindings")}</Alert>}
        <Findings findings={refused ?? payload.findings} />
      </Card>

      <ThresholdAssistant rows={rows} />

      <Card
        title={t("v2.dlc.criteria.rows")}
        end={
          mayEdit && !frozen ? (
            <Button
              size="sm"
              onClick={() => {
                setEntries((prev) => [...prev, ...editorRows([blank(prev.length)])]);
                setDirty(true);
              }}
            >
              <Plus size={14} /> {t("v2.dlc.criteria.addRow")}
            </Button>
          ) : undefined
        }
      >
        {frozen ? (
          <table className="v2-table">
            <thead>
              <tr>
                <th>{t("v2.dlc.criteria.key")}</th>
                <th>{t("v2.dlc.criteria.text")}</th>
                <th>{t("v2.dlc.criteria.dimension")}</th>
                <th>{t("v2.dlc.criteria.tier")}</th>
                <th>{t("v2.dlc.criteria.threshold")}</th>
                <th>{t("v2.dlc.criteria.executor")}</th>
              </tr>
            </thead>
            <tbody>
              {payload.criteria.map((row) => (
                <tr key={row.key}>
                  <td className="mono">{row.key}</td>
                  <td>{row.text}</td>
                  <td>{t(`v2.dlc.dimension.${row.dimension}`)}</td>
                  <td>
                    <TierChip tier={row.tier} effective={row.effective_tier} />
                  </td>
                  <td>
                    {row.executor.kind === "metric"
                      ? `${row.metric_rule?.op ?? "<="} ${row.metric_rule?.value ?? "—"}`
                      : row.threshold === null
                        ? "—"
                        : `${Math.round(row.threshold * 100)}%`}
                  </td>
                  <td className="mono">{row.executor.evaluator_id ?? row.executor.kind ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          entries.map(({ uid, row }) => (
            <RowEditor
              key={uid}
              row={row}
              onChange={(next) => update(uid, next)}
              onRemove={() => {
                setEntries((prev) => prev.filter((e) => e.uid !== uid));
                setDirty(true);
              }}
            />
          ))
        )}
        {!frozen && rows.length === 0 && <p className="v2-dlc-empty">{t("v2.dlc.criteria.noRows")}</p>}
      </Card>

      <div className="v2-dlc-actions">
        {!frozen && mayEdit && (
          <>
            <Button
              kind="primary"
              disabled={busy || !dirty}
              onClick={() => void run(() => dlcApi.saveSet(set.lineage_id, { criteria: rows }), "v2.dlc.criteria.saved")}
            >
              {t("v2.common.save")}
            </Button>
            <Button
              disabled={busy || dirty || errors.length > 0 || rows.length === 0}
              title={errors.length > 0 ? t("v2.dlc.criteria.fixFirst") : undefined}
              onClick={() => setConfirmPublish(true)}
            >
              {t("v2.dlc.criteria.publish")}
            </Button>
          </>
        )}
        {frozen && mayEdit && (
          <Button
            disabled={busy}
            onClick={() => void run(() => dlcApi.newSetVersion(set.lineage_id), "v2.dlc.criteria.newVersion")}
          >
            {t("v2.dlc.criteria.openNewVersion")}
          </Button>
        )}
        {frozen && !set.signed_by && maySign && (
          <Button kind="primary" disabled={busy} onClick={() => setSigning(true)}>
            {t("v2.dlc.criteria.sign")}
          </Button>
        )}
        {frozen && !set.signed_by && !maySign && (
          <span className="v2-muted">{t("v2.dlc.criteria.needsSigner")}</span>
        )}
        {signedBySelf && <span className="v2-muted">{t("v2.dlc.criteria.youSigned")}</span>}
      </div>

      <Card title={t("v2.dlc.criteria.versions")}>
        <table className="v2-table">
          <thead>
            <tr>
              <th>{t("v2.dlc.criteria.version")}</th>
              <th>{t("v2.common.status")}</th>
              <th>{t("v2.dlc.criteria.signedByCol")}</th>
              <th>{t("v2.dlc.criteria.publishedAt")}</th>
            </tr>
          </thead>
          <tbody>
            {payload.versions.map((v) => (
              <tr key={v.version} className={v.version === set.version ? "selected" : undefined}>
                <td>v{v.version}</td>
                <td>{t(`v2.dlc.criteria.status.${v.status}`)}</td>
                <td>{v.signed_by ?? "—"}</td>
                <td>{v.published_at ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Confirm
        open={confirmPublish}
        title={t("v2.dlc.criteria.publishTitle")}
        body={t("v2.dlc.criteria.publishText", { version: set.version })}
        confirmLabel={t("v2.dlc.criteria.publish")}
        busy={busy}
        onClose={() => setConfirmPublish(false)}
        onConfirm={() => {
          setConfirmPublish(false);
          void run(() => dlcApi.publishSet(set.lineage_id), "v2.dlc.criteria.published");
        }}
      />

      <Modal
        open={signing}
        title={t("v2.dlc.criteria.signTitle")}
        onClose={() => setSigning(false)}
        footer={
          <>
            <Button onClick={() => setSigning(false)}>{t("v2.common.cancel")}</Button>
            <Button
              kind="primary"
              disabled={busy}
              onClick={() => {
                setSigning(false);
                void run(() => dlcApi.signSet(set.lineage_id, signNote), "v2.dlc.criteria.signed");
              }}
            >
              {t("v2.dlc.criteria.sign")}
            </Button>
          </>
        }
      >
        <p className="v2-muted">{t("v2.dlc.criteria.signHint")}</p>
        <ul className="v2-dlc-signlist">
          {payload.criteria
            .filter((c) => c.tier !== "observe")
            .map((c) => (
              <li key={c.key}>
                <span className="mono">{c.key}</span>
                <TierChip tier={c.tier} effective={c.effective_tier} />
                <span>{c.text}</span>
              </li>
            ))}
        </ul>
        <Field label={t("v2.dlc.criteria.signNote")}>
          <textarea className="v2-input" rows={3} value={signNote} onChange={(e) => setSignNote(e.target.value)} />
        </Field>
      </Modal>
      {byKey.size === 0 && null}
    </>
  );
}
