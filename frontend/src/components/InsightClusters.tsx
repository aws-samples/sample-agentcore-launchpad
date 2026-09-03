import { useTranslation } from "react-i18next";

import type { InsightCluster, InsightTrees } from "../lib/evaluation";
import { Chip } from "./Chip";

function InsightSection({
  label,
  tone,
  icon,
  clusters,
  detail,
}: {
  label: string;
  tone: "crit" | "aqua" | "good";
  icon: string;
  clusters: InsightCluster[];
  detail: (c: InsightCluster) => string | undefined;
}) {
  const { t } = useTranslation();
  if (!clusters.length) return null;
  return (
    <>
      <div
        className="mono dim"
        style={{ fontSize: 9.5, letterSpacing: ".12em", margin: "10px 0 6px" }}
      >
        {label} · {clusters.length}
      </div>
      {clusters.slice(0, 3).map((c, i) => {
        const extra = detail(c);
        return (
          <div className="insight" key={c.clusterId ?? i}>
            <div className="ih">
              <Chip tone={tone} icon={icon}> </Chip>
              <b>{c.name ?? c.category ?? `#${i + 1}`}</b>
              <span className="pct">
                {typeof c.percentage === "number"
                  ? `${Math.round(c.percentage)}%`
                  : t("evalPage.insights.sessions", {
                      count: c.affectedSessionCount ?? c.affectedSessions?.length ?? 0,
                    })}
              </span>
            </div>
            {c.description && <div className="fix">{c.description.slice(0, 220)}</div>}
            {extra && (
              <div className="fix mono" style={{ color: "var(--ink-3)" }}>
                {extra.slice(0, 150)}
              </div>
            )}
          </div>
        );
      })}
    </>
  );
}

/**
 * Failure / user-intent / execution-summary clusters, three sections in that
 * order. Sections with no clusters render nothing — the caller decides what an
 * entirely empty tree set looks like (see `hasInsightTrees`).
 */
export function InsightClusters({ insights }: { insights: InsightTrees }) {
  const { t } = useTranslation();
  return (
    <>
      <InsightSection
        label={t("evalPage.insights.secFailures")}
        tone="crit"
        icon="✕"
        clusters={insights.failures ?? []}
        detail={(c) => {
          const rec = c.subCategories
            ?.flatMap((s) => s.rootCauses ?? [])
            .find((r) => r.recommendation)?.recommendation;
          return rec ? `⌁ ${rec}` : undefined;
        }}
      />
      <InsightSection
        label={t("evalPage.insights.secIntents")}
        tone="aqua"
        icon="◈"
        clusters={insights.userIntents ?? []}
        detail={(c) => {
          const msg = c.affectedSessions?.flatMap((s) => s.userMessages ?? [])[0];
          return msg ? `“${msg}”` : undefined;
        }}
      />
      <InsightSection
        label={t("evalPage.insights.secSummaries")}
        tone="good"
        icon="●"
        clusters={insights.executionSummaries ?? []}
        detail={(c) => c.affectedSessions?.find((s) => s.finalOutcome)?.finalOutcome}
      />
    </>
  );
}
