import { useTranslation } from "react-i18next";

import type { InsightCluster, InsightTrees } from "../lib/evaluation";
import { Tag, type TagTone } from "./ui";

function Section({
  label,
  tone,
  clusters,
  extra,
}: {
  label: string;
  tone: TagTone;
  clusters: InsightCluster[];
  extra: (c: InsightCluster) => string | undefined;
}) {
  const { t } = useTranslation();
  if (!clusters.length) return null;
  return (
    <>
      <h3 className="v2-sub-title">
        {label} · {clusters.length}
      </h3>
      <div className="v2-stack">
        {clusters.slice(0, 3).map((c, i) => {
          const more = extra(c);
          return (
            <div key={c.clusterId ?? i} className="v2-insight">
              <div className="h">
                <Tag tone={tone}>{c.name ?? c.category ?? `#${i + 1}`}</Tag>
                <span className="v2-muted">
                  {typeof c.percentage === "number"
                    ? `${Math.round(c.percentage)}%`
                    : t("evalPage.insights.sessions", { count: c.affectedSessionCount ?? c.affectedSessions?.length ?? 0 })}
                </span>
              </div>
              {c.description && <div>{c.description.slice(0, 220)}</div>}
              {more && <div className="v2-muted">{more.slice(0, 150)}</div>}
            </div>
          );
        })}
      </div>
    </>
  );
}

/** Failure / user-intent / execution-summary clusters (top three of each). */
export function InsightClusters({ insights }: { insights: InsightTrees }) {
  const { t } = useTranslation();
  return (
    <>
      <Section
        label={t("evalPage.insights.secFailures")}
        tone="red"
        clusters={insights.failures ?? []}
        extra={(c) => c.subCategories?.flatMap((s) => s.rootCauses ?? []).find((r) => r.recommendation)?.recommendation}
      />
      <Section
        label={t("evalPage.insights.secIntents")}
        tone="blue"
        clusters={insights.userIntents ?? []}
        extra={(c) => {
          const msg = c.affectedSessions?.flatMap((s) => s.userMessages ?? [])[0];
          return msg ? `“${msg}”` : undefined;
        }}
      />
      <Section
        label={t("evalPage.insights.secSummaries")}
        tone="green"
        clusters={insights.executionSummaries ?? []}
        extra={(c) => c.affectedSessions?.find((s) => s.finalOutcome)?.finalOutcome}
      />
    </>
  );
}
