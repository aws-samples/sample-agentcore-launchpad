import { LoaderCircle, Play } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Chip, Panel, useToast } from "../../components";
import {
  api,
  type GovernanceGatewayAction,
  type GovernancePolicyTestIdentity,
  type GovernancePolicyTestOutcome,
  type GovernancePolicyTestResult,
} from "../../lib/api";
import { governanceError } from "./types";

// ERROR is a non-decision (never recorded), so it must not share DENY's tone.
const OUTCOME_TONE: Record<GovernancePolicyTestOutcome, "good" | "crit" | "warn"> = {
  ALLOW: "good",
  DENY: "crit",
  ERROR: "warn",
};

const IDENTITIES: { value: GovernancePolicyTestIdentity; label: string }[] = [
  { value: "demo", label: "demo@hr-analyst" },
  { value: "admin", label: "admin@platform-admin" },
];

function preferredAction(actions: GovernanceGatewayAction[]): string {
  return (
    actions.find((action) => action.name === "hr-database___create_payout")?.name ??
    actions.find((action) => action.verified)?.name ??
    actions[0]?.name ??
    ""
  );
}

interface Props {
  actions: GovernanceGatewayAction[];
}

export function PolicyTestPanel({ actions }: Props) {
  const { t } = useTranslation();
  const toast = useToast();
  const orderedActions = useMemo(
    () =>
      [...actions].sort(
        (left, right) =>
          Number(right.verified) - Number(left.verified) ||
          left.name.localeCompare(right.name),
      ),
    [actions],
  );
  const [identity, setIdentity] = useState<GovernancePolicyTestIdentity>("demo");
  const [selectedTool, setSelectedTool] = useState(() => preferredAction(actions));
  const [argumentsText, setArgumentsText] = useState("{}");
  const [result, setResult] = useState<GovernancePolicyTestResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const effectiveTool = orderedActions.some((action) => action.name === selectedTool)
    ? selectedTool
    : preferredAction(orderedActions);
  const selectedAction = orderedActions.find((action) => action.name === effectiveTool);
  const requiredFields = Array.isArray(selectedAction?.input_schema?.required)
    ? selectedAction.input_schema.required.filter(
        (field): field is string => typeof field === "string",
      )
    : [];

  const parseArguments = (): Record<string, unknown> | null => {
    let value: unknown;
    try {
      value = JSON.parse(argumentsText.trim() || "{}");
    } catch {
      return null;
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    return value as Record<string, unknown>;
  };

  const runTest = async () => {
    if (!effectiveTool || running) return;
    const parsedArguments = parseArguments();
    if (parsedArguments === null) {
      const message = t("governance.policyTest.argumentsInvalid");
      setError(message);
      toast(message, "crit");
      return;
    }
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      setResult(
        await api.runGovernancePolicyTest({
          username: identity,
          tool: effectiveTool,
          arguments: parsedArguments,
        }),
      );
    } catch (requestError) {
      const message = governanceError(requestError);
      setError(message);
      toast(message, "crit");
    } finally {
      setRunning(false);
    }
  };

  return (
    <Panel
      title={t("governance.policyTest.title")}
      sub={t("governance.policyTest.source")}
      brk
    >
      <div className="gov-policy-test-controls">
        <div className="field">
          <label htmlFor="policy-test-identity">
            {t("governance.policyTest.identity")}
          </label>
          <select
            id="policy-test-identity"
            className="input"
            value={identity}
            disabled={running}
            onChange={(event) =>
              setIdentity(event.target.value as GovernancePolicyTestIdentity)
            }
          >
            {IDENTITIES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="policy-test-action">{t("governance.policyTest.action")}</label>
          <select
            id="policy-test-action"
            className="input mono"
            value={effectiveTool}
            disabled={running || orderedActions.length === 0}
            onChange={(event) => setSelectedTool(event.target.value)}
          >
            {orderedActions.length === 0 ? (
              <option value="">{t("governance.policyTest.noActions")}</option>
            ) : null}
            {orderedActions.map((action) => (
              <option key={action.name} value={action.name}>
                {action.name} /{" "}
                {action.verified
                  ? t("governance.states.verified")
                  : t("governance.states.unverified")}
              </option>
            ))}
          </select>
        </div>

        <Btn
          primary
          className="gov-policy-test-run"
          disabled={running || !effectiveTool}
          onClick={() => void runTest()}
        >
          {running ? (
            <LoaderCircle className="spin" size={14} aria-hidden="true" />
          ) : (
            <Play size={14} aria-hidden="true" />
          )}
          {running
            ? t("governance.policyTest.running")
            : t("governance.policyTest.run")}
        </Btn>
      </div>

      <div className="field gov-policy-test-arguments">
        <label htmlFor="policy-test-arguments">
          {t("governance.policyTest.arguments")}
        </label>
        <textarea
          id="policy-test-arguments"
          className="input mono"
          rows={3}
          value={argumentsText}
          disabled={running}
          onChange={(event) => setArgumentsText(event.target.value)}
          spellCheck={false}
        />
        {requiredFields.length > 0 ? (
          <div className="gov-cell-note mono">
            {t("governance.policyTest.requiredFields", {
              fields: requiredFields.join(", "),
            })}
          </div>
        ) : null}
      </div>

      {error ? <div className="gov-inline-error">{error}</div> : null}

      {result ? (
        <div className="gov-policy-test-result" aria-live="polite">
          <div className="gov-policy-test-result-head">
            <Chip tone={OUTCOME_TONE[result.outcome]}>{result.outcome}</Chip>
            <span className="mono">{result.principal}</span>
            <Chip tone={result.recorded ? "good" : "warn"}>
              {result.recorded
                ? t("governance.policyTest.recorded")
                : t("governance.policyTest.notRecorded")}
            </Chip>
          </div>
          <div className="gov-kv-list">
            <div className="kv">
              <span className="k">{t("governance.policyTest.exactAction")}</span>
              <span className="v mono gov-break">{result.tool}</span>
            </div>
            <div className="kv">
              <span className="k">{t("governance.policyTest.policy")}</span>
              <span className="v mono gov-break">{result.policy_id ?? "-"}</span>
            </div>
            <div className="kv">
              <span className="k">{t("governance.policyTest.decisionId")}</span>
              <span className="v mono gov-break">{result.decision_id ?? "-"}</span>
            </div>
          </div>
          <div className="field gov-policy-test-detail">
            <label>{t("governance.policyTest.detail")}</label>
            <pre className="code gov-code-wrap">{result.detail}</pre>
          </div>
        </div>
      ) : null}
    </Panel>
  );
}
