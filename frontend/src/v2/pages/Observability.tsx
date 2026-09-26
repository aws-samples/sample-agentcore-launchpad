import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { RANGES, rangeLabel } from "../format";
import { Button, FilterSelect, PageHeader, SubTabs } from "../ui";
import { asRange, asTab, OBS_TABS, type ObsTab } from "./observability/common";
import { Dashboard } from "./observability/Dashboard";
import { SessionDetail } from "./observability/SessionDetail";
import { SessionList } from "./observability/SessionList";
import { TraceDetail } from "./observability/TraceDetail";
import { TraceList } from "./observability/TraceList";
import "./observability/observability.css";

/**
 * 可观测 — AgentCore telemetry (aws/spans via Transaction Search): dashboard,
 * sessions and traces. Sub-pages are `?view=` states of this route:
 * `view=session&id=<session id>` and `view=trace&id=<trace id>`; `range=` is
 * shared by every tab and sub-page.
 */
export function V2Observability() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tab = asTab(params.get("tab"));
  const range = asRange(params.get("range"));
  const view = params.get("view");
  const id = params.get("id");
  // bumped by 刷新: the tab refetches with force=true (bypasses the backend cache)
  const [force, setForce] = useState(0);
  const [cacheHint, setCacheHint] = useState<string | null>(null);

  if (view === "session" && id) return <SessionDetail key={`${id}:${range}`} sessionId={id} range={range} />;
  if (view === "trace" && id) return <TraceDetail key={`${id}:${range}`} traceId={id} range={range} />;

  const go = (next: { tab?: ObsTab; range?: string }) => {
    const p: Record<string, string> = { tab: next.tab ?? tab };
    const r = next.range ?? range;
    if (r !== "24h") p.range = r;
    setParams(p);
  };

  return (
    <>
      <PageHeader
        title={t("nav.observability")}
        tabs={
          <SubTabs
            value={tab}
            onChange={(next) => go({ tab: next })}
            tabs={OBS_TABS.map((value) => ({ value, label: t(`v2.observability.tab.${value}`) }))}
          />
        }
        end={
          <>
            {cacheHint && <span className="v2-count">{cacheHint}</span>}
            <FilterSelect
              label={t("v2.common.timeRange")}
              value={range}
              onChange={(v) => go({ range: v })}
              options={RANGES.map((r) => ({ value: r, label: rangeLabel(t, r) }))}
              testId="v2-obs-range"
            />
            <Button onClick={() => setForce((n) => n + 1)} testId="v2-obs-refresh">
              <RefreshCw size={14} aria-hidden="true" />
              {t("v2.common.refresh")}
            </Button>
          </>
        }
      />
      {tab === "dashboard" && <Dashboard range={range} force={force} onCacheHint={setCacheHint} onForce={() => setForce((n) => n + 1)} />}
      {tab === "sessions" && <SessionList range={range} force={force} onCacheHint={setCacheHint} />}
      {tab === "traces" && <TraceList range={range} force={force} onCacheHint={setCacheHint} />}
    </>
  );
}
