// Building blocks shared by My usage (`/v2/usage`) and the admin Budgets tab: the spend
// bar against a limit, and the spend-by-source table.
import { useTranslation } from "react-i18next";

import type { BudgetTotals, UsageBySource } from "../../../lib/api";
import { barPercent, fmtPercent, fmtUsd, usageTone } from "../../../lib/budgets";
import { fmtNumber } from "../../format";
import { type Column, Table } from "../../ui";
import { useSourceLabel } from "./labels";

/** Used / limit as a bar and a percentage; unlimited reads as "Unlimited". */
export function UsageBar({ totals, testId }: { totals: BudgetTotals; testId?: string }) {
  const { t } = useTranslation();
  if (totals.unlimited) return <span className="v2-muted">{t("v2.usage.unlimited")}</span>;
  const tone = usageTone(totals);
  return (
    <span className="v2-usage-bar" data-testid={testId}>
      <span
        className={`v2-bar ${tone === "ok" ? "" : tone}`.trim()}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(barPercent(totals))}
        aria-label={t("v2.usage.percentUsed")}
      >
        <span style={{ width: `${barPercent(totals)}%` }} />
      </span>
      <span className={`pct ${tone === "ok" ? "" : tone}`.trim()}>{fmtPercent(totals.percent_used)}</span>
    </span>
  );
}

export function SourceTable({
  rows,
  loading,
  error,
  onRetry,
  testId,
}: {
  rows: UsageBySource[];
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  testId?: string;
}) {
  const { t } = useTranslation();
  const label = useSourceLabel();
  const columns: Column<UsageBySource>[] = [
    { key: "source", title: t("v2.usage.colSource"), render: (row) => label(row.source) },
    { key: "usd", title: t("v2.usage.colSpend"), className: "nowrap", render: (row) => fmtUsd(row.usd) },
    {
      key: "estimated",
      title: t("v2.usage.colEstimated"),
      className: "nowrap",
      render: (row) => (row.estimated_usd > 0 ? fmtUsd(row.estimated_usd) : <span className="v2-muted">—</span>),
    },
    { key: "events", title: t("v2.usage.colEvents"), className: "nowrap", render: (row) => fmtNumber(row.events) },
  ];
  return (
    <Table
      columns={columns}
      rows={rows}
      rowKey={(row) => row.source}
      loading={loading}
      error={error}
      onRetry={onRetry}
      empty={t("v2.usage.empty")}
      testId={testId}
    />
  );
}
