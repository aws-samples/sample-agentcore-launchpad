import { useTranslation } from "react-i18next";

import type { PolicyValidationIssue, PolicyValidationReport } from "../../../lib/api";
import { Alert, Tag } from "../../ui";
import { policyDecisionTone, policyModeTone, policyStatusTone } from "./common";

/** A gateway attachment mode (`LOG_ONLY` / `ENFORCE`) as a toned tag with its console label;
 *  historic decision rows still carry the retired workspace modes, read the same way. */
export function PolicyModeTag({ mode, testId }: { mode: string | null | undefined; testId?: string }) {
  const { t } = useTranslation();
  if (!mode) return <span className="v2-muted">—</span>;
  const key = mode.toLowerCase();
  return (
    <Tag tone={policyModeTone(mode)} dot testId={testId}>
      {t(`v2.policy.attachMode.${key}`, { defaultValue: mode })}
    </Tag>
  );
}

/** Policy row status (`draft` / `active` / `retired`). */
export function PolicyStatusTag({ status, testId }: { status: string | null | undefined; testId?: string }) {
  const { t } = useTranslation();
  if (!status) return <span className="v2-muted">—</span>;
  return (
    <Tag tone={policyStatusTone(status)} testId={testId}>
      {t(`v2.governance.policy.status.${status}`, { defaultValue: status })}
    </Tag>
  );
}

/** The compiled kind of a policy (`stateless` / `temporal`). */
export function PolicyKindTag({ kind, testId }: { kind: string | null | undefined; testId?: string }) {
  const { t } = useTranslation();
  if (!kind) return null;
  return (
    <Tag tone={kind === "temporal" ? "blue" : "gray"} testId={testId}>
      {t(`v2.governance.policy.kind.${kind}`, { defaultValue: kind })}
    </Tag>
  );
}

/** Global's per-policy `enforcementMode`: `active` reads 生效, `log_only` 仅记录. */
export function PolicyEnforcementTag({ mode, testId }: { mode: string | null | undefined; testId?: string }) {
  const { t } = useTranslation();
  if (!mode) return null;
  return (
    <Tag tone={mode === "log_only" ? "orange" : "green"} testId={testId}>
      {t(`v2.governance.policy.enforcement.${mode}`, { defaultValue: mode })}
    </Tag>
  );
}

/** An interceptor / simulator decision (`allow` / `deny` / `would_deny` / `error`). */
export function DecisionTag({ decision, title, testId }: { decision: string | null | undefined; title?: string; testId?: string }) {
  const { t } = useTranslation();
  if (!decision) return <span className="v2-muted">—</span>;
  const key = decision.toLowerCase();
  return (
    <Tag tone={policyDecisionTone(key === "no_decision" ? "error" : key)} title={title} testId={testId}>
      {t(`v2.governance.policy.decision.${key}`, { defaultValue: decision })}
    </Tag>
  );
}

const issueText = (issue: PolicyValidationIssue | string): string =>
  typeof issue === "string" ? issue : issue.line != null ? `${issue.line}:${issue.col ?? 0} ${issue.msg}` : issue.msg;

/** Server validation of a Cedar statement: errors with `line:col`, then warnings. */
export function ValidationReportView({ report }: { report: PolicyValidationReport | null }) {
  const { t } = useTranslation();
  if (!report) return null;
  return (
    <div className="v2-stack" data-testid="v2-policy-validation">
      {report.ok ? (
        <Alert tone="success">{t(report.scope === "gateway_active_set"
          ? "v2.governance.policy.validPublishPreflight"
          : "v2.governance.policy.validBasic")}</Alert>
      ) : (
        <Alert tone="error">
          <b>{t("v2.governance.policy.validFailed", { count: report.errors.length })}</b>
          <ul className="v2-policy-issues">
            {report.errors.map((issue, i) => (
              <li key={i} className="mono">
                {issueText(issue)}
              </li>
            ))}
          </ul>
        </Alert>
      )}
      {report.warnings.length > 0 && (
        <Alert tone="warn">
          <ul className="v2-policy-issues">
            {report.warnings.map((issue, i) => (
              <li key={i} className="mono">
                {issueText(issue)}
              </li>
            ))}
          </ul>
        </Alert>
      )}
    </div>
  );
}
