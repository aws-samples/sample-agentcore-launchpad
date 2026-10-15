import { RefreshCw } from "lucide-react";
import { useEffect, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api } from "../../lib/api";
import {
  basisKey,
  currentPeriod,
  fmtResetDate,
  fmtShare,
  fmtUsd,
  isExemptBasis,
  isPeriod,
  recentPeriods,
  usageTone,
} from "../../lib/budgets";
import { useLoad } from "../hooks";
import { primeMyUsage } from "../myUsage";
import { Alert, Button, Card, FilterSelect, Kpi, LinkButton, PageHeader } from "../ui";
import { SourceTable, UsageBar } from "./usage/UsageParts";

/**
 * 我的用量 — any signed-in person: their own spend in a calendar month (UTC) against
 * the monthly budget their level (or a personal amount) sets, by source. Reached from
 * the user menu; `?period=YYYY-MM` reads an earlier month. Admins and principals with
 * no account are unlimited unless an administrator assigns them a level.
 */
export function V2MyUsage() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const now = currentPeriod();
  const raw = params.get("period");
  const period = isPeriod(raw) ? raw : now;
  const periods = useMemo(() => {
    const list = recentPeriods();
    return list.includes(period) ? list : [period, ...list];
  }, [period]);
  const usage = useLoad(() => api.myUsage(period), `v2-my-usage:${period}`);
  const data = usage.data;

  // the shell banner shares this read of the current month
  useEffect(() => {
    if (data) primeMyUsage(data);
  }, [data]);

  const current = period === now;
  const tone = data ? usageTone(data) : "ok";
  const levelName = data?.level?.name ?? "";

  return (
    <>
      <PageHeader title={t("v2.usage.title")} desc={t("v2.usage.desc")} />
      <div className="v2-toolbar">
        <FilterSelect
          label={t("v2.usage.period")}
          value={period}
          onChange={(v) => setParams(v === now ? {} : { period: v })}
          options={periods.map((p) => ({ value: p, label: p === now ? t("v2.usage.thisMonth", { period: p }) : p }))}
          testId="v2-usage-period"
        />
        <Button onClick={usage.reload} disabled={usage.loading} testId="v2-usage-refresh">
          <RefreshCw size={14} aria-hidden="true" />
          {t("v2.common.refresh")}
        </Button>
      </div>
      {usage.error && (
        <Alert tone="error" action={<LinkButton onClick={usage.reload}>{t("v2.common.retry")}</LinkButton>}>
          <span data-testid="v2-usage-error">{usage.error}</span>
        </Alert>
      )}
      {data && current && tone === "over" && (
        <Alert tone="error">
          <span data-testid="v2-usage-over">
            {t("v2.usage.overBody", { date: fmtResetDate(data.resets_at) })}
          </span>
        </Alert>
      )}
      {data && current && tone === "warn" && (
        <Alert tone="warn">
          <span data-testid="v2-usage-warn">{t("v2.usage.warnBody", { date: fmtResetDate(data.resets_at) })}</span>
        </Alert>
      )}
      {data && (
        <Alert tone="info">
          <span data-testid={`v2-usage-basis-${data.basis}`}>{t(basisKey(data.basis), { level: levelName })}</span>
        </Alert>
      )}
      <div className="v2-kpis">
        <Kpi
          label={t("v2.usage.kpiLimit")}
          value={data ? (data.unlimited ? t("v2.usage.unlimited") : fmtUsd(data.limit_usd)) : "—"}
          sub={data?.level ? t("v2.usage.levelSub", { level: data.level.name }) : undefined}
          testId="v2-usage-kpi-limit"
        />
        <Kpi
          label={t("v2.usage.kpiUsed")}
          value={data ? fmtUsd(data.used_usd) : "—"}
          sub={data && data.estimated_usd > 0 ? t("v2.usage.estimatedSub", { share: fmtShare(data.estimated_share) }) : undefined}
          tone={tone === "over" ? "bad" : undefined}
          testId="v2-usage-kpi-used"
        />
        <Kpi
          label={t("v2.usage.kpiRemaining")}
          value={data ? (data.unlimited ? "—" : fmtUsd(data.remaining_usd)) : "—"}
          testId="v2-usage-kpi-remaining"
        />
        <Kpi
          label={t(current ? "v2.usage.kpiResets" : "v2.usage.kpiPeriod")}
          value={data ? (current ? fmtResetDate(data.resets_at) : data.period) : "—"}
          sub={current ? t("v2.usage.resetsSub") : undefined}
          testId="v2-usage-kpi-resets"
        />
      </div>
      {data && !isExemptBasis(data.basis) && (
        <Card title={t("v2.usage.progress")} testId="v2-usage-progress">
          <UsageBar totals={data} testId="v2-usage-bar" />
        </Card>
      )}
      <Card title={t("v2.usage.bySource")} sub={t("v2.usage.bySourceSub")} flush testId="v2-usage-by-source">
        <SourceTable
          rows={data?.by_source ?? []}
          loading={usage.loading}
          error={usage.error}
          onRetry={usage.reload}
          testId="v2-usage-sources"
        />
      </Card>
      <Alert tone="info">
        <span data-testid="v2-usage-note">{t("v2.usage.note")}</span>
      </Alert>
    </>
  );
}
