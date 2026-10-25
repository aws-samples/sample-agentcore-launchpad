import { useTranslation } from "react-i18next";

import type { OnlineQuality, OverviewInfo, TtfaInfo } from "../../../lib/api";
import { pct } from "../../format";
import { Panel, Stat } from "../../ui";
import { fmtSpan, qualitySignal } from "./modules";

/**
 * The Command center's second signals row (from the V2 Workbench): registry assets,
 * the evaluation pass rate, online quality over 24 h and — administrators only — the
 * workspace's median time to first agent. A tile that has not answered reads "—".
 */
export function MoreSignals({
  overview,
  quality,
  ttfa,
  isAdmin,
}: {
  overview: OverviewInfo | null;
  quality: OnlineQuality | null;
  ttfa: TtfaInfo | null;
  isAdmin: boolean;
}) {
  const { t } = useTranslation();
  const assets = overview?.registry_assets;
  const qSignal = qualitySignal(quality?.mean);
  return (
    <div className={isAdmin ? "v3-grid c4" : "v3-grid c3"} data-testid="v3-home-signals">
      <Panel>
        <Stat
          label={t("v2.home.kpi.assets")}
          value={assets ? assets.total : "—"}
          foot={assets ? t("overview.tiles.breakdown", { agents: assets.agents, tools: assets.tools, skills: assets.skills }) : undefined}
        />
      </Panel>
      <Panel>
        <Stat
          label={t("v2.home.kpi.passRate")}
          value={overview?.eval_pass_rate != null ? pct(overview.eval_pass_rate) : "—"}
          foot={overview
            ? overview.eval_runs > 0 ? t("overview.tiles.runCount", { count: overview.eval_runs }) : t("overview.tiles.noRuns")
            : undefined}
        />
      </Panel>
      <Panel signal={qSignal}>
        <Stat
          label={t("v2.home.kpi.quality")}
          value={quality?.mean != null ? pct(quality.mean) : "—"}
          signal={qSignal}
          foot={
            quality == null
              ? t("v2.home.kpi.last24h")
              : quality.configs === 0 && quality.scores === 0
                ? t("overview.tiles.onlineQualityNone")
                : quality.scores === 0
                  ? t("overview.tiles.onlineQualityPending")
                  : t("overview.tiles.onlineQualityFoot", { sessions: quality.sessions, agents: quality.agents })
          }
        />
      </Panel>
      {isAdmin && (
        <Panel>
          <Stat
            label={t("v2.home.kpi.ttfa")}
            value={ttfa?.median_seconds != null ? fmtSpan(t, ttfa.median_seconds) : "—"}
            foot={ttfa ? (ttfa.samples > 0 ? t("v2.home.kpi.ttfaSub", { count: ttfa.samples }) : t("v2.home.kpi.ttfaNone")) : undefined}
          />
        </Panel>
      )}
    </div>
  );
}
