import { Copy, RefreshCw, Search, Square, Tag, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { ErrorCallout } from "../../../components/ErrorCallout";
import { useAuth } from "../../../auth/auth-context";
import { api } from "../../../lib/api";
import { evalCostApi } from "../../../lib/api/evalCost";
import { useWorkspace } from "../../../workspace/workspace-context";
import {
  evaluatorSummary,
  loadRunTasks,
  sourceLabel,
  statusLabel,
  TASK_SOURCES,
  taskItemLabel,
  type TaskMode,
  type V2Task,
} from "../../../v2/tasks";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { permReason } from "../../reasons";
import { Btn, Chip, Confirm, Empty, Filters, Lamp, Notice, Panel, type Signal, Skeleton, Stat } from "../../ui";
import { calibrateBlocker, calibrateUrl } from "../standards/calibrationLink";
import { CopyId } from "./CopyId";
import { CostCell } from "./CostCard";
import { copyUrl, detailUrl, listCosts, rankTasks, runsWithoutCost, taskSignal } from "./model";
import { Select } from "../../select";

type Pending = { task: V2Task; action: "stop" | "delete" };
const PAGE = 25;


/**
 * 一次性评估 — the 评估任务 tab of batch evaluation runs (`loadRunTasks`: an online
 * config's on-demand insights reports stay on that config), led by what failed and
 * what is moving.
 */
export function RunList() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useToast();
  const { can } = useAuth();
  const { current } = useWorkspace();
  const list = useLoad(loadRunTasks, `v3-runs:${current?.id ?? ""}`);
  const [state, setState] = useState<"all" | Signal>("all");
  const [mode, setMode] = useState<"all" | TaskMode>("all");
  const [source, setSource] = useState("");
  const [agent, setAgent] = useState("");
  const [q, setQ] = useState("");
  const [shown, setShown] = useState(PAGE);
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  // a failed stop / delete / pause stays on the page with its way out (storyline-ux §4)
  const [failure, setFailure] = useState<unknown>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  // "usable for calibration": completed batch evaluator runs, the annotation-task candidates
  const [calibratable, setCalibratable] = useState(false);
  const mayRun = can("eval.run");
  // the same key as the criteria page's new-annotation-task button
  const mayCalibrate = can("criteria.manage");

  const tasks = useMemo(() => list.data ?? [], [list.data]);
  const agents = useMemo(() => [...new Set(tasks.map((task) => task.agentName))].sort(), [tasks]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return rankTasks(
      tasks.filter((task) => {
        if (state !== "all" && taskSignal(task.status) !== state) return false;
        if (mode !== "all" && task.mode !== mode) return false;
        if (source && task.source !== source) return false;
        if (agent && task.agentName !== agent) return false;
        if (calibratable && calibrateBlocker(task) !== null) return false;
        return !needle || `${task.name} ${task.id} ${task.description}`.toLowerCase().includes(needle);
      }),
    );
  }, [tasks, state, mode, source, agent, q, calibratable]);
  // the cost column (eval-cost §1.3): each run payload's own `cost`, one batch
  // request for the runs on screen an older server sent without it
  const visible = rows.slice(0, shown);
  const missingIds = runsWithoutCost(visible);
  const fetched = useLoad(() => evalCostApi.runCosts(missingIds), `v3-task-costs:${missingIds.join(",")}:${list.data ? "1" : "0"}`);
  const costs = listCosts(visible, fetched.data?.costs);
  const count = (s: Signal) => tasks.filter((task) => taskSignal(task.status) === s).length;
  const failed = rankTasks(tasks.filter((task) => task.status === "failed")).slice(0, 5);
  const modeLabel = (m: TaskMode) => t(m === "insights" ? "v2.tasks.modeInsightsShort" : "v2.tasks.modeEvaluators");

  const act = async () => {
    if (!pending) return;
    const { task, action } = pending;
    setBusy(true);
    setFailure(null);
    try {
      if (action === "stop") await api.stopEvaluationRun(task.id);
      else await api.deleteEvaluationRun(task.id);
      toast("ok", t(`v2.tasks.done.${action}`));
      setPending(null);
      list.reload();
    } catch (err) {
      setPending(null);
      setFailure(err);
    } finally {
      setBusy(false);
    }
  };

  const actions = (task: V2Task) => {
    const active = task.status === "running" || task.status === "queued";
    return (
      <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }} onClick={(e) => e.stopPropagation()}>
        {active && (
          <Btn size="sm" kind="ghost" disabled={!mayRun} disabledReason={permReason(t, "eval.run")}
            title={t("v2.tasks.stop")} aria-label={t("v2.tasks.stop")} onClick={() => setPending({ task, action: "stop" })}>
            <Square size={13} />
          </Btn>
        )}
        {mayCalibrate && (() => {
          const why = calibrateBlocker(task);
          return (
            <Btn size="sm" kind="ghost" disabled={why !== null} disabledReason={why ? t(`v3.tasks.calibrateBlocked.${why}`) : null}
              title={t("v3.tasks.calibrateShort")} aria-label={t("v3.tasks.calibrateShort")}
              onClick={() => task.agentId && navigate(calibrateUrl(task.agentId, task.id))}>
              <Tag size={13} />
            </Btn>
          );
        })()}
        <Btn size="sm" kind="ghost" disabled={!mayRun} disabledReason={permReason(t, "eval.run")}
          title={t("v2.tasks.copy")} aria-label={t("v2.tasks.copy")} onClick={() => navigate(copyUrl(task))}>
          <Copy size={13} />
        </Btn>
        <Btn size="sm" kind="ghost" disabled={!mayRun || active}
          disabledReason={!mayRun ? permReason(t, "eval.run") : t("story.reason.stopFirst")}
          title={t("v3.tasks.delete")} aria-label={t("v3.tasks.delete")}
          onClick={() => setPending({ task, action: "delete" })}>
          <Trash2 size={13} />
        </Btn>
      </div>
    );
  };

  return (
    <div className="v3-reveal v3-task" style={{ display: "grid", gap: 16 }}>
      <ErrorCallout error={failure} onDismiss={() => setFailure(null)} data-testid="v3-tasks-error" />

      <div className="v3-grid c3">
        <Panel signal={count("act") ? "act" : undefined}>
          <Stat label={t("v3.tasks.statFailed")} value={list.data ? count("act") : "—"} signal={count("act") ? "act" : undefined} foot={t("v3.tasks.statFailedFoot")} />
        </Panel>
        <Panel signal={count("info") ? "info" : undefined}>
          <Stat label={t("v3.tasks.statRunning")} value={list.data ? count("info") : "—"} foot={t("v3.tasks.statRunningFoot", { count: count("wait") })} />
        </Panel>
        <Panel><Stat label={t("v3.tasks.statDone")} value={list.data ? count("ok") : "—"} foot={t("v3.tasks.statDoneFoot")} /></Panel>
      </div>

      {failed.length > 0 && (
        <Panel title={t("v3.tasks.attention")} signal="act" flush end={<span className="mono">{count("act")}</span>}>
          <table className="v3-table">
            <tbody>
              {failed.map((task) => (
                <tr key={`${task.kind}:${task.id}`} className="click" onClick={() => navigate(detailUrl(task))}>
                  <td style={{ width: 30 }}><Lamp s="act" /></td>
                  <td>
                    <div className="v3-name">
                      <div>
                        <b>{task.name}</b>
                        <small style={{ color: "var(--v3-act)" }}>{(task.run?.error || task.online?.failure_reason || statusLabel(t, task.status)).slice(0, 140)}</small>
                      </div>
                    </div>
                  </td>
                  <td style={{ color: "var(--v3-text-2)" }}>{task.agentName}</td>
                  <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(task.updatedAt)}</td>
                  <td style={{ width: 1, whiteSpace: "nowrap" }}>{actions(task)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters
          value={state}
          onChange={(v) => { setState(v); setShown(PAGE); }}
          options={[
            { value: "all", label: t("v3.tasks.all"), count: tasks.length },
            { value: "act", label: statusLabel(t, "failed"), s: "act", count: count("act") },
            { value: "info", label: statusLabel(t, "running"), s: "info", count: count("info") },
            { value: "wait", label: t("v3.tasks.waiting"), s: "wait", count: count("wait") },
            { value: "ok", label: statusLabel(t, "completed"), s: "ok", count: count("ok") },
            { value: "off", label: statusLabel(t, "stopped"), s: "off", count: count("off") },
          ]}
        />
        <span style={{ width: 1, height: 20, background: "var(--v3-line)" }} aria-hidden="true" />
        <Filters
          value={mode}
          onChange={(v) => { setMode(v); setShown(PAGE); }}
          options={[
            { value: "all", label: t("v3.tasks.anyMode") },
            { value: "evaluators", label: modeLabel("evaluators") },
            { value: "insights", label: modeLabel("insights") },
          ]}
        />
        <Select className="v3-select" style={{ width: 150 }} value={source} onChange={(e) => setSource(e.target.value)} aria-label={t("v2.tasks.colSource")}>
          <option value="">{t("v3.tasks.anySource")}</option>
          {TASK_SOURCES.map((s) => <option key={s} value={s}>{t(`v2.taskSource.${s}Short`)}</option>)}
        </Select>
        <Select className="v3-select" style={{ width: 170 }} value={agent} onChange={(e) => setAgent(e.target.value)} aria-label={t("v2.tasks.colAgent")}>
          <option value="">{t("v3.tasks.anyAgent")}</option>
          {agents.map((a) => <option key={a} value={a}>{a}</option>)}
        </Select>
        <label style={{ display: "inline-flex", gap: 6, alignItems: "center", color: "var(--v3-text-2)" }}>
          <input type="checkbox" checked={calibratable} onChange={(e) => { setCalibratable(e.target.checked); setShown(PAGE); }} />
          <span>{t("v3.tasks.filterCalibratable")}</span>
        </label>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center", flex: "1 1 240px", maxWidth: 320 }}>
          <Btn kind="ghost" onClick={list.reload} title={t("v3.tasks.refresh")} aria-label={t("v3.tasks.refresh")}><RefreshCw size={14} /></Btn>
          <div style={{ position: "relative", flex: 1 }}>
          <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
          <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => setQ(e.target.value)}
            placeholder={t("v2.tasks.search")} aria-label={t("v2.tasks.search")} />
          </div>
        </div>
      </div>

      <Panel flush>
        {list.loading && !list.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={6} /></div>
        ) : list.error ? (
          <div style={{ padding: 20 }}><Notice s="act"><ApiErrorDetail error={list.failure ?? list.error} /></Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={tasks.length ? t("v3.tasks.none") : t("v2.tasks.empty")}>{!tasks.length && t("v3.tasks.emptySub")}</Empty>
        ) : (
          <>
            <table className="v3-table">
              <thead>
                <tr>
                  <th />
                  <th>{t("v3.tasks.colTask")}</th>
                  <th>{t("v2.tasks.mode")}</th>
                  <th>{t("v2.tasks.colAgent")}</th>
                  <th>{t("v2.tasks.colSource")}</th>
                  <th>{t("v2.tasks.colEvaluators")}</th>
                  <th className="num">{t("v3.evalCost.colCost")}</th>
                  <th className="num">{t("v3.tasks.colCreated")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.slice(0, shown).map((task) => {
                  const s = taskSignal(task.status);
                  const label = (id: string) => taskItemLabel(t, task, id);
                  const rowKey = `${task.kind}:${task.id}`;
                  return (
                    <tr key={rowKey} className="click" onClick={() => navigate(detailUrl(task))}
                      onMouseEnter={() => setHovered(rowKey)} onMouseLeave={() => setHovered((k) => (k === rowKey ? null : k))}
                      onFocus={() => setHovered(rowKey)}>
                      <td style={{ width: 30 }}><Lamp s={s} live={s === "info"} title={statusLabel(t, task.status)} /></td>
                      <td>
                        <div className="v3-name">
                          <div>
                            <b className="v3-task-clip" title={task.name}>{task.name}</b>
                            <small>{statusLabel(t, task.status)} · <CopyId id={task.id} kind={task.kind} quiet={hovered !== rowKey} /></small>
                          </div>
                        </div>
                      </td>
                      <td><Chip s={task.mode === "insights" ? "info" : undefined}>{modeLabel(task.mode)}</Chip></td>
                      <td>
                        <span className={task.logSource ? "mono v3-task-clip" : "v3-task-clip"} title={task.agentName}>{task.agentName}</span>
                        {task.logSource && <small className="v3-task-sub">{t("v2.tasks.cw.short")}</small>}
                      </td>
                      <td>
                        <span className="v3-task-clip" title={sourceLabel(t, task)}>{sourceLabel(t, task)}</span>
                      </td>
                      <td style={{ color: "var(--v3-text-2)" }}>
                        <span className="v3-task-clip" title={task.evaluators.map(label).join("、")}>{evaluatorSummary(t, task.evaluators, label)}</span>
                      </td>
                      <td className="num">
                        <CostCell summary={costs[task.id]} />
                      </td>
                      <td className="num" style={{ color: "var(--v3-text-3)" }} title={task.updatedAt ?? undefined}>{ago(task.createdAt)}</td>
                      <td style={{ width: 1, whiteSpace: "nowrap" }}>{actions(task)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {rows.length > shown && (
              <div style={{ padding: 14, display: "flex", justifyContent: "center" }}>
                <Btn size="sm" onClick={() => setShown((n) => n + PAGE)}>{t("v3.tasks.more", { count: rows.length - shown })}</Btn>
              </div>
            )}
          </>
        )}
      </Panel>

      {pending && (
        <Confirm
          title={t(`v2.tasks.confirm.${pending.action}Title`)}
          confirmLabel={t(`v2.tasks.confirm.${pending.action}Ok`)}
          cancelLabel={t("v3.common.cancel")}
          danger={pending.action === "delete" || pending.action === "stop"}
          busy={busy}
          onCancel={() => setPending(null)}
          onConfirm={() => void act()}
        >
          {t(`v2.tasks.confirm.${pending.action}Body`, { name: pending.task.name })}
        </Confirm>
      )}
    </div>
  );
}
