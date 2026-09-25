import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, errorMessage, type ObsDashboard, type V2Range } from "../../../lib/api";
import { fmtBucket, fmtCompact } from "../../../pages/observability/format";
import { fmtDuration, fmtNumber } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Card, Kpi, LinkButton, Spin } from "../../ui";
import { approxCost, useReportCache } from "./common";

function Legend({ items }: { items: { color: string; label: string }[] }) {
  return (
    <div className="v2-observability-legend">
      {items.map((item) => (
        <span key={item.label}>
          <i style={{ background: item.color }} />
          {item.label}
        </span>
      ))}
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="v2-observability-empty">{text}</div>;
}

function TrafficChart({ data }: { data: ObsDashboard }) {
  const { t } = useTranslation();
  const series = data.series;
  const max = Math.max(1, ...series.map((b) => b.traces));
  const longRange = data.range === "7d";
  return (
    <Card title={t("v2.observability.chart.traffic")} sub={t("v2.observability.chart.trafficSub")} testId="v2-obs-traffic">
      {series.length === 0 ? (
        <Empty text={t("v2.observability.chart.noData")} />
      ) : (
        <>
          <div className="v2-observability-bars">
            {series.map((b) => (
              <div
                key={b.bucket}
                className="b"
                style={{ height: `${Math.max(2, (b.traces / max) * 100)}%` }}
                title={t("v2.observability.chart.bucketTip", {
                  bucket: fmtBucket(b.bucket, longRange),
                  traces: b.traces,
                  errors: b.errors,
                })}
              >
                {b.errors > 0 && <div className="e" style={{ height: `${(b.errors / b.traces) * 100}%` }} />}
              </div>
            ))}
          </div>
          <div className="v2-observability-axis">
            <span>{fmtBucket(series[0].bucket, longRange)}</span>
            {series.length > 2 && <span>{fmtBucket(series[Math.floor(series.length / 2)].bucket, longRange)}</span>}
            <span>{t("v2.observability.chart.now")}</span>
          </div>
          <Legend
            items={[
              { color: "var(--v2-primary)", label: t("v2.observability.chart.traces") },
              { color: "var(--v2-danger)", label: t("v2.observability.chart.errors") },
            ]}
          />
        </>
      )}
    </Card>
  );
}

function LatencyChart({ data }: { data: ObsDashboard }) {
  const { t } = useTranslation();
  const series = data.series;
  const max = Math.max(1, ...series.map((b) => b.p95_ms));
  const W = 560;
  const TOP = 12;
  const BASE = 132;
  const x = (i: number) => (series.length > 1 ? (i / (series.length - 1)) * W : W / 2);
  const y = (v: number) => BASE - (v / max) * (BASE - TOP);
  const points = (pick: (b: ObsDashboard["series"][number]) => number) =>
    series.map((b, i) => `${x(i).toFixed(1)},${y(pick(b)).toFixed(1)}`).join(" ");
  return (
    <Card title={t("v2.observability.chart.latency")} sub={t("v2.observability.chart.latencySub")} testId="v2-obs-latency">
      {series.length === 0 ? (
        <Empty text={t("v2.observability.chart.noData")} />
      ) : (
        <>
          <svg className="v2-observability-line" viewBox={`0 0 ${W} ${BASE + 8}`} preserveAspectRatio="none" aria-hidden="true">
            <line x1="0" y1={BASE} x2={W} y2={BASE} className="axis" />
            <line x1="0" y1={(BASE + TOP) / 2} x2={W} y2={(BASE + TOP) / 2} className="grid" />
            <line x1="0" y1={TOP} x2={W} y2={TOP} className="grid" />
            <polyline fill="none" stroke="var(--v2-warning)" strokeWidth="2" vectorEffect="non-scaling-stroke" points={points((b) => b.p95_ms)} />
            <polyline fill="none" stroke="var(--v2-primary)" strokeWidth="2" vectorEffect="non-scaling-stroke" points={points((b) => b.p50_ms)} />
          </svg>
          <div className="v2-observability-axis">
            <span>{t("v2.observability.chart.peak", { v: fmtDuration(max) })}</span>
            <span>{t("v2.observability.chart.now")}</span>
          </div>
          <Legend
            items={[
              { color: "var(--v2-primary)", label: `P50 · ${fmtDuration(data.tiles.latency.p50_ms)}` },
              { color: "var(--v2-warning)", label: `P95 · ${fmtDuration(data.tiles.latency.p95_ms)}` },
            ]}
          />
        </>
      )}
    </Card>
  );
}

function TokensByModel({ data, onPricesRefreshed }: { data: ObsDashboard; onPricesRefreshed: () => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [busy, setBusy] = useState(false);
  const rows = data.tokens_by_model;
  const grand = Math.max(1, ...rows.map((r) => r.total));

  const refreshPrices = () => {
    setBusy(true);
    api
      .obsRefreshPrices()
      .then((res) => {
        toast("success", t("obs.prices.refreshed", { updated: res.meta.updated.length, added: res.meta.added.length }));
        onPricesRefreshed();
      })
      .catch((err: unknown) => toast("error", t("obs.loadFailed", { msg: errorMessage(err) })))
      .finally(() => setBusy(false));
  };

  return (
    <Card
      title={t("v2.observability.chart.tokens")}
      sub={t("v2.observability.chart.tokensSub")}
      end={
        <LinkButton disabled={busy} onClick={refreshPrices} testId="v2-obs-refresh-prices">
          {busy ? t("v2.observability.chart.pricesUpdating") : t("v2.observability.chart.pricesUpdate")}
        </LinkButton>
      }
      testId="v2-obs-tokens"
    >
      {rows.length === 0 ? (
        <Empty text={t("v2.observability.chart.noTokens")} />
      ) : (
        <>
          {rows.map((r) => (
            <div className="v2-observability-hbar" key={r.model}>
              <div className="l">
                <span title={r.model}>{r.model}</span>
                <b>
                  {fmtCompact(r.total)} · {approxCost(r.est_cost_usd)}
                </b>
              </div>
              <div className="track" title={t("v2.observability.chart.inOut", { input: fmtNumber(r.input), output: fmtNumber(r.output) })}>
                <div className="seg" style={{ width: `${(r.input / grand) * 100}%`, background: "var(--v2-primary)" }} />
                <div className="seg" style={{ width: `${(r.output / grand) * 100}%`, background: "#14c9c9" }} />
              </div>
            </div>
          ))}
          <Legend
            items={[
              { color: "var(--v2-primary)", label: t("v2.observability.chart.input") },
              { color: "#14c9c9", label: t("v2.observability.chart.output") },
            ]}
          />
          <div className="v2-observability-note">
            {t("obs.charts.priceNote")}
            {data.prices_meta?.updated_at != null &&
              ` · ${t("obs.prices.updatedAt", { date: data.prices_meta.updated_at.slice(0, 10) })}`}
          </div>
        </>
      )}
    </Card>
  );
}

function TopTools({ data }: { data: ObsDashboard }) {
  const { t } = useTranslation();
  const rows = data.top_tools;
  const max = Math.max(1, ...rows.map((r) => r.calls));
  return (
    <Card title={t("v2.observability.chart.tools")} sub={t("v2.observability.chart.toolsSub")} testId="v2-obs-tools">
      {rows.length === 0 ? (
        <Empty text={t("v2.observability.chart.noTools")} />
      ) : (
        <>
          {rows.slice(0, 8).map((r) => (
            <div className="v2-observability-hbar" key={r.tool}>
              <div className="l">
                <span title={r.tool}>{r.tool}</span>
                <b>
                  {t("v2.observability.chart.calls", { count: r.calls })}
                  {r.success_rate != null && ` · ${r.success_rate}%`}
                </b>
              </div>
              <div className="track">
                <div className="seg" style={{ width: `${((r.calls - r.errors) / max) * 100}%`, background: "var(--v2-success)" }} />
                {r.errors > 0 && <div className="seg" style={{ width: `${(r.errors / max) * 100}%`, background: "var(--v2-danger)" }} />}
              </div>
            </div>
          ))}
          <Legend
            items={[
              { color: "var(--v2-success)", label: t("v2.observability.chart.success") },
              { color: "var(--v2-danger)", label: t("v2.observability.chart.failed") },
            ]}
          />
        </>
      )}
    </Card>
  );
}

export function Dashboard({
  range,
  force,
  onCacheHint,
  onForce,
}: {
  range: V2Range;
  force: number;
  onCacheHint: (hint: string | null) => void;
  onForce: () => void;
}) {
  const { t } = useTranslation();
  const { data, loading, error, reload } = useLoad(() => api.obsDashboard(range, force > 0), `obs-dash:${range}:${force}`);
  useReportCache(data?.cache.age_seconds, data, loading, onCacheHint);

  if (error && !data) {
    return (
      <Alert tone="error" action={<LinkButton onClick={reload}>{t("v2.common.retry")}</LinkButton>}>
        {t("obs.loadFailed", { msg: error })}
      </Alert>
    );
  }
  if (!data) return <Spin />;

  const { tiles } = data;
  const errPct = (tiles.error_rate * 100).toFixed(1);
  return (
    <>
      {error && <Alert tone="error">{t("obs.loadFailed", { msg: error })}</Alert>}
      <div className="v2-kpis">
        <Kpi
          label={t("v2.observability.kpi.traces")}
          value={fmtNumber(tiles.traces.total)}
          sub={t("v2.observability.kpi.tracesSub", { ok: tiles.traces.ok, error: tiles.traces.error })}
          testId="v2-obs-kpi-traces"
        />
        <Kpi
          label={t("v2.observability.kpi.sessions")}
          value={fmtNumber(tiles.sessions.total)}
          sub={t("v2.observability.kpi.agentsActive", { count: tiles.sessions.agents })}
        />
        <Kpi
          label={t("v2.observability.kpi.errorRate")}
          value={`${errPct}%`}
          tone={tiles.error_rate > 0.05 ? "bad" : undefined}
          sub={t("v2.observability.kpi.errorOf", { errors: tiles.traces.error, total: tiles.traces.total })}
        />
        <Kpi
          label={t("v2.observability.kpi.latency")}
          value={
            <>
              {fmtDuration(tiles.latency.p50_ms)}
              <small className="v2-observability-unit"> / {fmtDuration(tiles.latency.p95_ms)}</small>
            </>
          }
          sub={t("v2.observability.kpi.latencySub")}
        />
        <Kpi
          label={t("v2.observability.kpi.tokens")}
          value={fmtCompact(tiles.tokens.total)}
          sub={t("v2.observability.kpi.tokensSub", {
            input: fmtCompact(tiles.tokens.input),
            output: fmtCompact(tiles.tokens.output),
            cost: approxCost(tiles.tokens.est_cost_usd),
          })}
        />
      </div>
      <div className="v2-grid-2 v2-observability-charts">
        <TrafficChart data={data} />
        <LatencyChart data={data} />
      </div>
      <div className="v2-grid-2 v2-observability-charts">
        <TokensByModel data={data} onPricesRefreshed={onForce} />
        <TopTools data={data} />
      </div>
    </>
  );
}
