import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  type GovernanceGatewayAction,
  type GovernancePolicyTestIdentity,
  type GovernancePolicyTestOutcome,
  type GovernancePolicyTestResult,
} from "../../../lib/api";
import {
  governanceError,
  parseArgumentsObject,
  POLICY_TEST_IDENTITIES,
  preferredTestAction,
  requiredSchemaFields,
  sortTestActions,
} from "../../../lib/governance";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Card, Descriptions, Field, Tag, type TagTone } from "../../ui";

// ERROR is a non-decision (never recorded), so it must not share DENY's tone.
const OUTCOME_TONE: Record<GovernancePolicyTestOutcome, TagTone> = { ALLOW: "green", DENY: "red", ERROR: "orange" };

/** A real tools/call through the configured Launchpad Gateway as a demo identity. */
export function PolicyTestCard({ actions }: { actions: GovernanceGatewayAction[] }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const ordered = useMemo(() => sortTestActions(actions), [actions]);
  const [identity, setIdentity] = useState<GovernancePolicyTestIdentity>("demo");
  const [selectedTool, setSelectedTool] = useState(() => preferredTestAction(actions));
  const [argumentsText, setArgumentsText] = useState("{}");
  const [result, setResult] = useState<GovernancePolicyTestResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const tool = ordered.some((action) => action.name === selectedTool) ? selectedTool : preferredTestAction(ordered);
  const required = requiredSchemaFields(ordered.find((action) => action.name === tool));

  const run = async () => {
    if (!tool || running) return;
    const args = parseArgumentsObject(argumentsText);
    if (args === null) {
      const message = t("governance.policyTest.argumentsInvalid");
      setError(message);
      toast("error", message);
      return;
    }
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      setResult(await api.runGovernancePolicyTest({ username: identity, tool, arguments: args }));
    } catch (requestError) {
      const message = governanceError(requestError);
      setError(message);
      toast("error", message);
    } finally {
      setRunning(false);
    }
  };

  return (
    <Card
      title={t("v2.governance.policyTest")}
      sub={t("governance.policyTest.source")}
      end={
        <Button kind="primary" size="sm" disabled={running || !tool} onClick={() => void run()} testId="v2-governance-policy-test-run">
          {running ? t("v2.governance.testRunning") : t("v2.governance.runTest")}
        </Button>
      }
      testId="v2-governance-policy-test"
    >
      <div className="v2-form cols-2">
        <Field label={t("v2.governance.testIdentity")}>
          <select
            className="v2-select"
            value={identity}
            disabled={running}
            onChange={(e) => setIdentity(e.target.value as GovernancePolicyTestIdentity)}
          >
            {POLICY_TEST_IDENTITIES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t("v2.governance.exactAction")}>
          <select
            className="v2-select mono"
            value={tool}
            disabled={running || ordered.length === 0}
            onChange={(e) => setSelectedTool(e.target.value)}
          >
            {ordered.length === 0 && <option value="">{t("v2.governance.noActions")}</option>}
            {ordered.map((action) => (
              <option key={action.name} value={action.name}>
                {action.name} · {action.verified ? t("v2.governance.verified") : t("v2.governance.unverified")}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label={t("v2.governance.arguments")}
          full
          hint={required.length > 0 ? t("governance.policyTest.requiredFields", { fields: required.join(", ") }) : undefined}
        >
          <textarea
            className="v2-textarea code"
            rows={3}
            value={argumentsText}
            disabled={running}
            spellCheck={false}
            onChange={(e) => setArgumentsText(e.target.value)}
          />
        </Field>
      </div>
      {error && (
        <div style={{ marginTop: 12 }}>
          <Alert tone="error">{error}</Alert>
        </div>
      )}
      {result && (
        <div className="v2-governance-test-result" aria-live="polite">
          <div className="v2-row" style={{ marginBottom: 12 }}>
            <Tag tone={OUTCOME_TONE[result.outcome]}>{result.outcome}</Tag>
            <span className="mono">{result.principal}</span>
            <Tag tone={result.recorded ? "green" : "orange"}>
              {result.recorded ? t("v2.governance.recorded") : t("v2.governance.notRecorded")}
            </Tag>
          </div>
          <Descriptions
            items={[
              { label: t("v2.governance.exactAction"), value: <span className="mono">{result.tool}</span> },
              { label: t("v2.governance.determiningPolicy"), value: <span className="mono">{result.policy_id ?? "—"}</span> },
              { label: t("v2.governance.decisionId"), value: <span className="mono">{result.decision_id ?? "—"}</span> },
            ]}
          />
          <div className="v2-sub-title">{t("v2.governance.rawDetail")}</div>
          <pre className="v2-pre">{result.detail}</pre>
        </div>
      )}
    </Card>
  );
}
