import { useTranslation } from "react-i18next";

import type { PolicySimulateResult } from "../../../lib/api";
import { Alert, Button, Descriptions, Field, Select, Tag } from "../../ui";
import { emptyHistoryRow, type HistoryRow } from "./engineHistory";
import { DecisionTag } from "./widgets";

const KINDS: HistoryRow["kind"][] = ["request", "response", "error"];

/** The history table of the engine simulation: add prior events (tool, kind, input, output,
 *  minutes before the request). */
export function EngineHistoryEditor({
  rows,
  tools,
  disabled,
  onChange,
}: {
  rows: HistoryRow[];
  tools: string[];
  disabled: boolean;
  onChange: (rows: HistoryRow[]) => void;
}) {
  const { t } = useTranslation();
  const set = (i: number, patch: Partial<HistoryRow>) => onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const toolOptions = tools.map((name) => ({ value: name, label: name }));
  return (
    <div className="v2-stack" data-testid="v2-policy-sim-history">
      {rows.length === 0 && <div className="v2-muted" style={{ fontSize: 12.5 }}>{t("v2.governance.policy.sim.engine.historyEmpty")}</div>}
      {rows.map((row, i) => (
        <div key={i} className="v2-form cols-2" data-testid={`v2-policy-sim-history-row-${i}`}>
          <Field label={t("v2.governance.policy.sim.engine.historyTool")}>
            {toolOptions.length > 0 ? (
              <Select
                mono
                value={row.tool}
                disabled={disabled}
                placeholder={t("v2.governance.policy.sim.noCandidates")}
                options={toolOptions}
                onChange={(tool) => set(i, { tool })}
                testId={`v2-policy-sim-history-tool-${i}`}
              />
            ) : (
              <input
                className="v2-input mono"
                value={row.tool}
                disabled={disabled}
                spellCheck={false}
                onChange={(e) => set(i, { tool: e.target.value })}
                data-testid={`v2-policy-sim-history-tool-${i}`}
              />
            )}
          </Field>
          <Field label={t("v2.governance.policy.sim.engine.historyKind")}>
            <Select
              value={row.kind}
              disabled={disabled}
              options={KINDS.map((kind) => ({ value: kind, label: t(`v2.governance.policy.sim.engine.kind.${kind}`) }))}
              onChange={(kind) => set(i, { kind: kind as HistoryRow["kind"] })}
              testId={`v2-policy-sim-history-kind-${i}`}
            />
          </Field>
          <Field label={t("v2.governance.policy.sim.engine.historyInput")}>
            <textarea
              className="v2-textarea code"
              rows={2}
              value={row.input}
              disabled={disabled}
              spellCheck={false}
              onChange={(e) => set(i, { input: e.target.value })}
              data-testid={`v2-policy-sim-history-input-${i}`}
            />
          </Field>
          <Field label={t("v2.governance.policy.sim.engine.historyOutput")}>
            <textarea
              className="v2-textarea code"
              rows={2}
              value={row.output}
              disabled={disabled}
              spellCheck={false}
              onChange={(e) => set(i, { output: e.target.value })}
              data-testid={`v2-policy-sim-history-output-${i}`}
            />
          </Field>
          <Field label={t("v2.governance.policy.sim.engine.historyMinutes")}>
            <input
              className="v2-input mono"
              type="number"
              min={0}
              value={row.minutesBefore}
              disabled={disabled}
              onChange={(e) => set(i, { minutesBefore: e.target.value })}
              data-testid={`v2-policy-sim-history-minutes-${i}`}
            />
          </Field>
          <div className="v2-field">
            <Button size="sm" disabled={disabled} onClick={() => onChange(rows.filter((_r, j) => j !== i))} testId={`v2-policy-sim-history-remove-${i}`}>
              {t("v2.governance.policy.sim.engine.historyRemove")}
            </Button>
          </div>
        </div>
      ))}
      <div>
        <Button size="sm" disabled={disabled} onClick={() => onChange([...rows, emptyHistoryRow(tools[0] ?? "")])} testId="v2-policy-sim-history-add">
          {t("v2.governance.policy.sim.engine.historyAdd")}
        </Button>
      </div>
    </div>
  );
}

/** The engine's answer: Stage A (the agent may), Stage BT (on behalf of the calling
 *  principal, temporal rules included), the final decision, the determining policies and
 *  the replayed trace. */
export function EngineSimResult({ result }: { result: PolicySimulateResult }) {
  const { t } = useTranslation();
  const stage = (value: string, testId: string) => <DecisionTag decision={value} testId={testId} />;
  return (
    <div className="v2-governance-test-result" aria-live="polite" data-testid="v2-policy-sim-engine-result">
      <div className="v2-policy-outcome">
        {stage(result.final, "v2-policy-sim-engine-final")}
        <Tag tone="outline">{t(`v2.governance.policy.sim.engine.sourceSet`, { set: result.source_set })}</Tag>
        {result.decision_id ? (
          <Tag tone="green">{t("v2.governance.policy.sim.recorded")}</Tag>
        ) : (
          <Tag tone="gray">{t("v2.governance.policy.sim.notRecorded")}</Tag>
        )}
      </div>
      <Descriptions
        items={[
          { label: t("v2.governance.policy.sim.engine.stageA"), value: stage(result.stage_a, "v2-policy-sim-engine-stage-a") },
          { label: t("v2.governance.policy.sim.engine.stageBt"), value: stage(result.stage_bt, "v2-policy-sim-engine-stage-bt") },
          {
            label: t("v2.governance.policy.sim.determining"),
            value: result.determining.length ? (
              <div className="v2-tags" data-testid="v2-policy-sim-engine-determining">
                {result.determining.map((id) => (
                  <Tag key={id} tone="outline">
                    <span className="mono">{id}</span>
                  </Tag>
                ))}
              </div>
            ) : (
              <span className="v2-muted">{t("v2.governance.policy.sim.defaultDeny")}</span>
            ),
          },
          ...(result.log_only_would
            ? [{ label: t("v2.governance.policy.sim.engine.logOnlyWould"), value: <DecisionTag decision={result.log_only_would} /> }]
            : []),
        ]}
      />
      {result.trace.length > 0 && (
        <div className="v2-stack">
          <b>{t("v2.governance.policy.sim.engine.trace")}</b>
          <ol className="v2-policy-issues" data-testid="v2-policy-sim-engine-trace">
            {result.trace.map((entry, i) => (
              <li key={i} className="mono">
                {entry.tool} · {t(`v2.governance.policy.sim.engine.kind.${entry.kind}`, { defaultValue: entry.kind })}
                {entry.result?.decision ? ` → ${entry.result.decision}` : ""}
                {entry.result?.code ? ` → ${entry.result.code}` : ""}
              </li>
            ))}
          </ol>
        </div>
      )}
      {result.warnings.length > 0 && (
        <Alert tone="warn">
          <ul className="v2-policy-issues">
            {result.warnings.map((line, i) => (
              <li key={i}>{line}</li>
            ))}
          </ul>
        </Alert>
      )}
      {result.errors.length > 0 && (
        <Alert tone="error">
          <ul className="v2-policy-issues">
            {result.errors.map((line, i) => (
              <li key={i} className="mono">
                {line}
              </li>
            ))}
          </ul>
        </Alert>
      )}
    </div>
  );
}
