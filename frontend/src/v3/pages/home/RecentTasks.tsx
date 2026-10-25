import { ArrowRight } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { statusLabel, type V2Task } from "../../../v2/tasks";
import { ago } from "../../format";
import { Chip, Empty, Lamp, Notice, Panel, Skeleton } from "../../ui";
import { detailUrl, taskSignal } from "../tasks/model";

const SHOWN = 5;

/** The newest evaluation tasks (batch runs and agent-owned online configs), each opening its V3 detail. */
export function RecentTasks({
  tasks,
  loading,
  error,
  onRetry,
}: {
  tasks: V2Task[] | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  const rows = (tasks ?? []).slice(0, SHOWN);
  const running = (tasks ?? []).filter((task) => task.status === "running").length;
  return (
    <div data-testid="v3-home-tasks">
      <Panel
        title={t("v2.home.recentTasks")}
        end={<Link to="/v3/tasks" className="v3-btn ghost sm">{t("v2.home.allTasks")} <ArrowRight size={13} aria-hidden="true" /></Link>}
        flush
      >
        {loading && !tasks ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : error && !tasks ? (
          <div style={{ padding: 16 }}>
            <Notice s="act">{error}{" "}
              <button type="button" className="v3-btn sm" onClick={onRetry}>{t("v3.ux.retry")}</button>
            </Notice>
          </div>
        ) : rows.length === 0 ? (
          <Empty title={t("v2.tasks.empty")}>
            <Link to="/v3/tasks?view=new" className="v3-btn sm">{t("v2.home.step.evaluate")}</Link>
          </Empty>
        ) : (
          <table className="v3-table">
            <tbody>
              {rows.map((task) => {
                const s = taskSignal(task.status);
                return (
                  <tr key={`${task.kind}:${task.id}`}>
                    <td style={{ width: 30 }}><Lamp s={s} live={s === "info"} title={statusLabel(t, task.status)} /></td>
                    <td>
                      <div className="v3-name">
                        <div>
                          <Link className="v3-inline-link" to={detailUrl(task)} title={task.name}><b>{task.name}</b></Link>
                          <small>{task.agentName}</small>
                        </div>
                      </div>
                    </td>
                    <td><Chip s={s === "off" ? undefined : s}>{statusLabel(t, task.status)}</Chip></td>
                    <td className="num" style={{ color: "var(--v3-text-3)", width: 70 }}>{ago(task.createdAt)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {running > 0 && (
          <div style={{ padding: "8px 16px 12px", color: "var(--v3-text-3)", fontSize: 12 }}>
            {t("v2.home.kpiTasksSub", { count: running })}
          </div>
        )}
      </Panel>
    </div>
  );
}
