// The reconciliation half of the Budgets tab (`/v2/costs?view=budgets`, admin): whether
// the background reconciliation runs and how far behind it is, this month's spend by kind
// (measured vs estimated), the hourly Runtime calibration factors, and a report-only
// comparison with Cost Explorer that is loaded only when an administrator asks — each
// uncached read costs $0.01 per AWS account (services/budget_calibration.py).
import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type CalibrationReport, errorMessage, type ReconcileStatus } from "../../../lib/api";
import { fmtShare, fmtUsd } from "../../../lib/budgets";
import { fmtTime } from "../../format";
import { SIGNAL_TONE } from "../../signals";
import { Alert, Button, Card, type Column, Descriptions, LinkButton, Table, Tag } from "../../ui";
import {
  type FactorRow,
  factorRows,
  type KindRow,
  kindRows,
  lagMinutes,
  RECONCILE_SIGNAL,
  reconcileHealth,
  reportShare,
} from "./budgets";

function fmtFactor(value: number | null | undefined, digits = 3): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return String(Number(value.toFixed(digits)));
}

export function ReconcileCard({
  status,
  loading,
  error,
  onRetry,
}: {
  status: ReconcileStatus | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  if (!status) {
    return (
      <Card title={t("v2.budgets.reconcile.title")} sub={t("v2.budgets.reconcile.sub")} testId="v2-budget-reconcile">
        {error ? (
          <Alert tone="error" action={<LinkButton onClick={onRetry}>{t("v2.common.retry")}</LinkButton>}>
            {error}
          </Alert>
        ) : (
          <span className="v2-muted">{loading ? t("v2.common.loading") : "—"}</span>
        )}
      </Card>
    );
  }
  const health = reconcileHealth(status);
  const tick = status.last_tick;
  const lag = lagMinutes(status.lag_seconds);
  const kinds = kindRows(status.by_kind);
  const columns: Column<KindRow>[] = [
    {
      key: "kind",
      title: t("v2.budgets.reconcile.colKind"),
      render: (row) => (
        <>
          <span>{t(`v2.budgets.kind.${row.kind}`, { defaultValue: row.kind })}</span>
          {row.agentcore && <span className="sub">{t("v2.budgets.reconcile.agentcoreFee")}</span>}
        </>
      ),
    },
    { key: "usd", title: t("v2.budgets.reconcile.colTotal"), className: "nowrap", render: (row) => fmtUsd(row.usd) },
    {
      key: "measured",
      title: t("v2.budgets.reconcile.colMeasured"),
      className: "nowrap",
      render: (row) => fmtUsd(row.measuredUsd),
    },
    {
      key: "estimated",
      title: t("v2.budgets.reconcile.colEstimated"),
      className: "nowrap",
      render: (row) => (row.usd > 0 ? fmtShare(row.estimatedShare) : "—"),
    },
  ];

  return (
    <Card
      title={t("v2.budgets.reconcile.title")}
      sub={t("v2.budgets.reconcile.sub", { minutes: Math.round(status.interval_seconds / 60) })}
      testId="v2-budget-reconcile"
      end={
        <Tag tone={SIGNAL_TONE[RECONCILE_SIGNAL[health]]} dot testId="v2-budget-reconcile-health">
          {t(`v2.budgets.reconcile.health.${health}`)}
        </Tag>
      }
    >
      <div className="v2-stack">
        {health === "off" && (
          <Alert tone="warn">
            <span data-testid="v2-budget-reconcile-off">{t("v2.budgets.reconcile.offNote")}</span>
          </Alert>
        )}
        <Descriptions
          items={[
            { label: t("v2.budgets.reconcile.lastRun"), value: tick ? fmtTime(tick.finished_at) : t("v2.budgets.reconcile.never") },
            {
              label: t("v2.budgets.reconcile.lag"),
              value: lag == null ? "—" : t("v2.budgets.reconcile.lagValue", { count: lag }),
            },
            { label: t("v2.budgets.reconcile.pending"), value: String(status.pending_sessions) },
            {
              label: t("v2.budgets.reconcile.lastTick"),
              value: tick
                ? t("v2.budgets.reconcile.lastTickValue", {
                    sessions: tick.sessions,
                    rows: tick.rows,
                    superseded: tick.superseded,
                    seconds: tick.duration_s,
                  })
                : "—",
            },
            { label: t("v2.budgets.reconcile.replaced"), value: fmtUsd(status.superseded_usd) },
            {
              label: t("v2.budgets.reconcile.paused"),
              value: tick ? String(tick.paused) : "—",
            },
          ]}
        />
        {tick && tick.failures.length > 0 && (
          <div data-testid="v2-budget-reconcile-failures">
            <Alert tone="error">
              <div className="v2-stack">
                <span>{t("v2.budgets.reconcile.failures", { count: tick.failures.length })}</span>
                {tick.failures.slice(0, 5).map((f, index) => (
                  <span key={`${f.workspace_id}-${f.part}-${index}`} className="mono">
                    {[f.workspace_id ?? "—", f.part ?? ""].filter(Boolean).join(" · ")}: {f.error}
                  </span>
                ))}
              </div>
            </Alert>
          </div>
        )}
        <span className="v2-sec-title">{t("v2.budgets.reconcile.byKind", { period: status.period })}</span>
        <span className="v2-muted">{t("v2.budgets.reconcile.byKindHint")}</span>
      </div>
      <Table
        columns={columns}
        rows={kinds}
        rowKey={(row) => row.kind}
        empty={t("v2.budgets.reconcile.byKindEmpty")}
        testId="v2-budget-reconcile-kinds"
      />
    </Card>
  );
}

function ReportTable({ report }: { report: CalibrationReport }) {
  const { t } = useTranslation();
  return (
    <div className="v2-stack" data-testid="v2-budget-report">
      {report.accounts.map((account) => {
        const share = reportShare(account);
        const columns: Column<CalibrationReport["accounts"][number]["rows"][number]>[] = [
          {
            key: "category",
            title: t("v2.budgets.calibration.colCategory"),
            render: (row) => t(`v2.budgets.calibration.category.${row.category}`, { defaultValue: row.category }),
          },
          {
            key: "ledger",
            title: t("v2.budgets.calibration.colLedger"),
            className: "nowrap",
            render: (row) => (row.category === "overhead" ? "—" : fmtUsd(row.ledger_usd)),
          },
          {
            key: "ce",
            title: t("v2.budgets.calibration.colCostExplorer"),
            className: "nowrap",
            render: (row) => fmtUsd(row.cost_explorer_usd),
          },
        ];
        return (
          <div key={account.account_id} className="v2-stack">
            <span className="v2-sec-title">
              {t("v2.budgets.calibration.account", {
                account: account.account_id,
                regions: account.workspaces.map((ws) => ws.region).join(", "),
              })}
            </span>
            {account.error ? (
              <Alert tone="error">{t("v2.budgets.calibration.accountError", { error: account.error })}</Alert>
            ) : (
              <span className="v2-muted">
                {t("v2.budgets.calibration.totals", {
                  ledger: fmtUsd(account.ledger_total_usd),
                  bill: fmtUsd(account.cost_explorer_total_usd),
                  share: share == null ? "—" : fmtShare(share),
                })}
              </span>
            )}
            <Table
              columns={columns}
              rows={account.rows}
              rowKey={(row) => row.category}
              empty={t("v2.budgets.calibration.reportEmpty")}
              testId={`v2-budget-report-${account.account_id}`}
            />
          </div>
        );
      })}
      <span className="v2-muted">
        {t(report.cached ? "v2.budgets.calibration.generatedCached" : "v2.budgets.calibration.generated", {
          when: fmtTime(report.generated_at),
        })}
      </span>
    </div>
  );
}

export function CalibrationCard({ status }: { status: ReconcileStatus | null }) {
  const { t } = useTranslation();
  const [report, setReport] = useState<CalibrationReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rows = status ? factorRows(status) : [];
  const cal = status?.calibration;

  // never on page load: Cost Explorer bills each uncached read
  const load = async (refresh: boolean) => {
    setBusy(true);
    setError(null);
    try {
      setReport(await api.budgetCalibrationReport(refresh));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<FactorRow>[] = [
    {
      key: "runtime",
      title: t("v2.budgets.calibration.colRuntime"),
      render: (row) => (
        <>
          <span className="mono">{row.runtime_name}</span>
          <span className="sub">{t(row.harness ? "v2.budgets.calibration.harness" : "v2.budgets.calibration.runtime")}</span>
        </>
      ),
    },
    {
      key: "gb",
      title: t("v2.budgets.calibration.colGb"),
      className: "nowrap",
      render: (row) => (
        <>
          <span>{fmtFactor(row.gb, 2)}</span>
          <span className="sub">{t("v2.budgets.calibration.seed", { value: fmtFactor(row.seedGb, 2) })}</span>
        </>
      ),
    },
    {
      key: "vcpu",
      title: t("v2.budgets.calibration.colVcpu"),
      className: "nowrap",
      render: (row) => (
        <>
          <span>{fmtFactor(row.vcpu)}</span>
          <span className="sub">{t("v2.budgets.calibration.seed", { value: fmtFactor(row.seedVcpu) })}</span>
        </>
      ),
    },
    {
      key: "sessions",
      title: t("v2.budgets.calibration.colSessions"),
      className: "nowrap",
      render: (row) => t("v2.budgets.calibration.sessionsValue", { sessions: row.sessions, hours: row.hours }),
    },
    {
      key: "used",
      title: t("v2.budgets.calibration.colUsed"),
      className: "nowrap",
      render: (row) => (
        <Tag tone={row.used ? "green" : "gray"} testId={`v2-budget-factor-used-${row.runtime_id}`}>
          {t(row.used ? "v2.budgets.calibration.used" : "v2.budgets.calibration.seedUsed")}
        </Tag>
      ),
    },
  ];

  return (
    <Card
      title={t("v2.budgets.calibration.title")}
      sub={t("v2.budgets.calibration.sub", { count: cal?.min_sessions ?? 3 })}
      testId="v2-budget-calibration"
      end={
        <Button size="sm" disabled={busy} onClick={() => void load(report !== null)} testId="v2-budget-report-load">
          <RefreshCw size={13} aria-hidden="true" />
          {t(report ? "v2.budgets.calibration.refreshReport" : "v2.budgets.calibration.loadReport")}
        </Button>
      }
    >
      <div className="v2-stack">
        <span className="v2-muted" data-testid="v2-budget-calibration-last">
          {cal?.at
            ? t("v2.budgets.calibration.lastRun", {
                hour: fmtTime(cal.last_hour),
                when: fmtTime(cal.at),
                samples: cal.samples ?? 0,
              })
            : t("v2.budgets.calibration.neverRun")}
        </span>
        {(cal?.failures?.length ?? 0) > 0 && (
          <Alert tone="error">
            {t("v2.budgets.calibration.failures", { count: cal?.failures?.length ?? 0 })}{" "}
            <span className="mono">{cal?.failures?.[0]?.error}</span>
          </Alert>
        )}
      </div>
      <Table
        columns={columns}
        rows={rows}
        rowKey={(row) => row.runtime_id}
        empty={t("v2.budgets.calibration.empty")}
        testId="v2-budget-factors"
      />
      <div className="v2-stack" style={{ marginTop: 16 }}>
        <Alert tone="info">
          <span data-testid="v2-budget-report-note">{t("v2.budgets.calibration.reportNote")}</span>
        </Alert>
        {error && (
          <div data-testid="v2-budget-report-error">
            <Alert tone="error">{error}</Alert>
          </div>
        )}
        {report && <ReportTable report={report} />}
      </div>
    </Card>
  );
}
