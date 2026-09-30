import { Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { hasInsightTrees, type InsightCluster } from "../lib/evaluation";
import type { V2Task } from "./tasks";
import { Button, Card, Descriptions, Drawer, LinkButton, Segmented, Spin, Tag, type TagTone } from "./ui";

type Section = "failures" | "userIntents" | "executionSummaries";
const SECTIONS: Section[] = ["failures", "userIntents", "executionSummaries"];
const TONE: Record<Section, TagTone> = { failures: "red", userIntents: "blue", executionSummaries: "green" };
const LABEL_KEY: Record<Section, string> = {
  failures: "v2.insights.panel.failures",
  userIntents: "v2.insights.panel.intents",
  executionSummaries: "v2.insights.panel.summaries",
};
/** Clusters listed before "N more". */
const TOP = 6;

/** One cluster name across every insights task in scope (runs name clusters independently). */
interface MergedCluster {
  key: string;
  section: Section;
  name: string;
  description: string;
  sessions: number;
  sessionIds: string[];
  recommendation: string | null;
  members: { task: V2Task; cluster: InsightCluster }[];
}

const clusterName = (c: InsightCluster, i: number) => (c.name ?? c.category ?? `#${i + 1}`).trim();
const sessionCount = (c: InsightCluster) => c.affectedSessionCount ?? c.affectedSessions?.length ?? 0;

/** Merge the three insight trees of every task, biggest cluster first. */
function mergeClusters(tasks: V2Task[]): Record<Section, MergedCluster[]> {
  const out: Record<Section, MergedCluster[]> = { failures: [], userIntents: [], executionSummaries: [] };
  for (const section of SECTIONS) {
    const byKey = new Map<string, MergedCluster>();
    for (const task of tasks) {
      (task.run?.insights?.[section] ?? []).forEach((cluster, i) => {
        const name = clusterName(cluster, i);
        const key = `${section}:${name.toLowerCase()}`;
        const merged =
          byKey.get(key) ??
          byKey
            .set(key, { key, section, name, description: "", sessions: 0, sessionIds: [], recommendation: null, members: [] })
            .get(key)!;
        merged.sessions += sessionCount(cluster);
        merged.description ||= cluster.description ?? "";
        merged.recommendation ||=
          cluster.subCategories?.flatMap((s) => s.rootCauses ?? []).find((r) => r.recommendation)?.recommendation ?? null;
        for (const s of cluster.affectedSessions ?? []) {
          if (s.sessionId && !merged.sessionIds.includes(s.sessionId)) merged.sessionIds.push(s.sessionId);
        }
        merged.members.push({ task, cluster });
      });
    }
    out[section] = [...byKey.values()].sort((a, b) => b.sessions - a.sessions || a.name.localeCompare(b.name));
  }
  return out;
}

function Stat({ label, value, sub, tone }: { label: string; value: number; sub?: string; tone?: "bad" }) {
  return (
    <div className="v2-istat">
      <div className="l">{label}</div>
      <div className={`v ${tone ?? ""}`}>{value}</div>
      {sub && <div className="s">{sub}</div>}
    </div>
  );
}

function ClusterDrawer({ cluster, onClose }: { cluster: MergedCluster; onClose: () => void }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const tasks = [...new Map(cluster.members.map((m) => [m.task.id, m.task])).values()];
  const subCategories = cluster.members.flatMap((m) => m.cluster.subCategories ?? []);
  const affected = cluster.members.flatMap((m) => m.cluster.affectedSessions ?? []);
  return (
    <Drawer
      open
      onClose={onClose}
      title={
        <span className="v2-row">
          <Tag tone={TONE[cluster.section]}>{t(LABEL_KEY[cluster.section])}</Tag>
          {cluster.name}
        </span>
      }
      testId="v2-insight-drawer"
    >
      <div className="v2-stack">
        <Descriptions
          one
          items={[
            { label: t("v2.traces.colSession"), value: t("v2.insights.panel.sessions", { count: cluster.sessions }) },
            {
              label: t("v2.insights.panel.sourceTasks"),
              value: (
                <div className="v2-stack" style={{ gap: 4, alignItems: "flex-start" }}>
                  {tasks.map((task) => (
                    <LinkButton key={task.id} onClick={() => navigate(`/v2/eval/tasks?view=detail&kind=run&id=${encodeURIComponent(task.id)}`)}>
                      {task.name} · {task.agentName}
                    </LinkButton>
                  ))}
                </div>
              ),
            },
          ]}
        />
        {cluster.description && <p style={{ margin: 0, lineHeight: 1.6 }}>{cluster.description}</p>}
        {subCategories.length > 0 && (
          <>
            <h3 className="v2-sub-title">{t("v2.insights.panel.subCategories")}</h3>
            {subCategories.map((sub, i) => (
              <div key={i} className="v2-insight">
                <strong>{sub.name ?? `#${i + 1}`}</strong>
                {(sub.rootCauses ?? []).map((cause, j) => (
                  <div key={j}>
                    <span className="v2-ilabel">{t("v2.insights.panel.rootCauses")}</span>
                    {cause.name ?? "—"}
                    {cause.recommendation && (
                      <div className="v2-irec full">
                        <span className="v2-ilabel">{t("v2.insights.panel.recommendation")}</span>
                        {cause.recommendation}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ))}
          </>
        )}
        {affected.length > 0 && (
          <>
            <h3 className="v2-sub-title">
              {t("v2.insights.panel.affected")} · {affected.length}
            </h3>
            {affected.slice(0, 20).map((s, i) => (
              <div key={`${s.sessionId}:${i}`} className="v2-insight">
                <span className="mono v2-muted">{s.sessionId ?? "—"}</span>
                {s.userMessages?.[0] && (
                  <div>
                    <span className="v2-ilabel">{t("v2.insights.panel.userMessage")}</span>“{s.userMessages[0]}”
                  </div>
                )}
                {s.approachTaken && (
                  <div>
                    <span className="v2-ilabel">{t("v2.insights.panel.approach")}</span>
                    {s.approachTaken}
                  </div>
                )}
                {s.finalOutcome && (
                  <div>
                    <span className="v2-ilabel">{t("v2.insights.panel.outcome")}</span>
                    {s.finalOutcome}
                  </div>
                )}
              </div>
            ))}
          </>
        )}
      </div>
    </Drawer>
  );
}

/**
 * 评估洞察 — the insights side of the evaluation overview: every completed
 * insights task in scope, its failure / user-intent / execution-summary
 * clusters merged by name, biggest first; a cluster opens its sub-categories,
 * root causes with recommendations, affected sessions and source tasks.
 */
export function InsightsPanel({ tasks, loading, needle }: { tasks: V2Task[]; loading: boolean; needle: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [section, setSection] = useState<Section>("failures");
  const [expanded, setExpanded] = useState(false);
  const [open, setOpen] = useState<MergedCluster | null>(null);

  const merged = useMemo(() => mergeClusters(tasks), [tasks]);
  const withTrees = tasks.filter((task) => hasInsightTrees(task.run?.insights)).length;
  const failedSessions = useMemo(() => {
    const ids = new Set(merged.failures.flatMap((c) => c.sessionIds));
    return ids.size || merged.failures.reduce((n, c) => n + c.sessions, 0);
  }, [merged]);
  const q = needle.trim().toLowerCase();
  const list = merged[section].filter((c) => !q || `${c.name} ${c.description}`.toLowerCase().includes(q));
  const shown = expanded ? list : list.slice(0, TOP);
  const max = Math.max(1, ...list.map((c) => c.sessions));

  return (
    <Card
      title={t("v2.insights.panel.title")}
      sub={t("v2.insights.panel.sub")}
      end={
        <Button onClick={() => navigate("/v2/eval/tasks?view=new")} testId="v2-insights-new">
          <Plus size={14} aria-hidden="true" />
          {t("v2.insights.panel.newTask")}
        </Button>
      }
      testId="v2-insights-panel"
    >
      {loading && tasks.length === 0 ? (
        <Spin />
      ) : tasks.length === 0 ? (
        <p className="v2-muted">{t("v2.insights.panel.empty")}</p>
      ) : (
        <>
          <div className="v2-istats">
            <Stat label={t("v2.insights.panel.tasks")} value={tasks.length} sub={t("v2.insights.panel.tasksSub", { count: withTrees })} />
            <Stat
              label={t("v2.insights.panel.failures")}
              value={merged.failures.length}
              tone={merged.failures.length ? "bad" : undefined}
              sub={t("v2.insights.panel.failuresSub", { count: failedSessions })}
            />
            <Stat label={t("v2.insights.panel.intents")} value={merged.userIntents.length} />
            <Stat label={t("v2.insights.panel.summaries")} value={merged.executionSummaries.length} />
          </div>
          {withTrees === 0 ? (
            <p className="v2-muted">{t("v2.insights.panel.emptyTrees")}</p>
          ) : (
            <>
              <div style={{ margin: "16px 0 12px" }}>
                <Segmented
                  value={section}
                  onChange={(s) => {
                    setSection(s);
                    setExpanded(false);
                  }}
                  options={SECTIONS.map((s) => ({ value: s, label: `${t(LABEL_KEY[s])} · ${merged[s].length}` }))}
                  ariaLabel={t("v2.insights.panel.title")}
                />
              </div>
              {list.length === 0 ? (
                <p className="v2-muted">{t("v2.common.empty")}</p>
              ) : (
                <div className="v2-iclusters">
                  {shown.map((c, i) => {
                    const taskCount = new Set(c.members.map((m) => m.task.id)).size;
                    return (
                      <button type="button" key={c.key} className="v2-icluster" onClick={() => setOpen(c)} data-testid="v2-insight-cluster">
                        <span className={`rank ${TONE[c.section]}`}>{i + 1}</span>
                        <span className="body">
                          <span className="name">{c.name}</span>
                          {c.description && <span className="desc">{c.description}</span>}
                          {c.recommendation && (
                            <span className="v2-irec">
                              <span className="v2-ilabel">{t("v2.insights.panel.recommendation")}</span>
                              {c.recommendation}
                            </span>
                          )}
                        </span>
                        <span className="meter">
                          <span className="v2-bar">
                            <span style={{ width: `${Math.round((c.sessions / max) * 100)}%`, background: `var(--v2-${c.section === "failures" ? "danger" : c.section === "userIntents" ? "primary" : "success"})` }} />
                          </span>
                          <span className="v2-muted">
                            {t("v2.insights.panel.sessions", { count: c.sessions })} · {t("v2.insights.panel.inTasks", { count: taskCount })}
                          </span>
                        </span>
                      </button>
                    );
                  })}
                  {list.length > TOP && (
                    <div>
                      <LinkButton onClick={() => setExpanded(!expanded)}>
                        {expanded ? t("v2.common.collapse") : t("v2.insights.panel.more", { count: list.length - TOP })}
                      </LinkButton>
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </>
      )}
      {open && <ClusterDrawer cluster={open} onClose={() => setOpen(null)} />}
    </Card>
  );
}
