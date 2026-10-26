import "../online.css";

import { Pause, Play, RefreshCw, Search, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api, type OnlineEvalConfigRow } from "../../../lib/api";
import { evaluatorLabel } from "../../../lib/evaluators";
import { agentLabel, canToggle, configName, isEditable, isTransient, POLL_MS } from "../../../v2/online";
import { evaluatorSummary } from "../../../v2/tasks";
import { useWorkspace } from "../../../workspace/workspace-context";
import { ago } from "../../format";
import { useLoad } from "../../hooks";
import { permReason } from "../../reasons";
import { Select } from "../../select";
import { Btn, Chip, Empty, Filters, Lamp, Notice, Panel, type Signal, Skeleton, Stat } from "../../ui";
import { onlineHref, useOnlineAction } from "./actions";
import { attentionOf, configSignal, modeOf } from "./common";

/**
 * 持续评估 — the 评估任务 tab (the former 在线评估 page): every online config that
 * judges live traffic (agent-owned, experiment arms, external), scores and insights
 * modes, led by the configs that need a person.
 */
export function OnlineList() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { can } = useAuth();
  const mayRun = can("eval.run");
  const { current } = useWorkspace();
  const { data, loading, error, reload } = useLoad(() => api.v2OnlineConfigs(), `v3-online:${current?.id ?? ""}`);
  const action = useOnlineAction(() => reload());
  const [state, setState] = useState<"all" | Signal>("all");
  const [mode, setMode] = useState<"all" | "scores" | "insights">("all");
  // agent-owned configs first, as in V2: experiment arms and external configs are a filter away
  const [owner, setOwner] = useState<"all" | OnlineEvalConfigRow["owner"]>("agent");
  const [agent, setAgent] = useState("");
  const [q, setQ] = useState("");

  const configs = useMemo(() => data?.configs ?? [], [data]);
  const agents = useMemo(() => [...new Set(configs.map(agentLabel))].sort(), [configs]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return configs.filter((row) => {
      if (state !== "all" && configSignal(row) !== state) return false;
      if (mode !== "all" && modeOf(row) !== mode) return false;
      if (owner !== "all" && row.owner !== owner) return false;
      if (agent && agentLabel(row) !== agent) return false;
      return !needle || `${configName(row)} ${row.config_id} ${row.description}`.toLowerCase().includes(needle);
    });
  }, [configs, state, mode, owner, agent, q]);
  const attention = configs.filter((row) => attentionOf(row) !== null);
  const count = (s: Signal) => configs.filter((row) => configSignal(row) === s).length;

  // CREATING / UPDATING / DELETING settle on their own: follow them
  const transient = configs.some(isTransient);
  useEffect(() => {
    if (!transient) return;
    const timer = window.setInterval(reload, POLL_MS);
    return () => window.clearInterval(timer);
  }, [transient, reload]);

  const open = (row: OnlineEvalConfigRow) => navigate(onlineHref({ view: "detail", id: row.config_id }));
  const judges = (row: OnlineEvalConfigRow) =>
    modeOf(row) === "insights"
      ? t("v2.online.insightsCount", { count: row.insights.length })
      : row.detailed
        ? evaluatorSummary(t, row.evaluators, (id) => evaluatorLabel(t, id))
        : "—";

  return (
    <div className="v3-reveal v3-onl" style={{ display: "grid", gap: 16 }}>
      <div className="v3-grid c4">
        <Panel><Stat label={t("v3.online.configs")} value={data ? configs.length : "—"} foot={t("v3.online.configsFoot")} /></Panel>
        <Panel signal={count("ok") ? "ok" : undefined}><Stat label={t("v3.online.watching")} value={data ? count("ok") : "—"} foot={t("v3.online.watchingFoot")} /></Panel>
        <Panel><Stat label={t("v3.online.paused")} value={data ? count("off") : "—"} foot={t("v3.online.pausedFoot")} /></Panel>
        <Panel signal={attention.length ? (attention.some((r) => attentionOf(r) === "failed") ? "act" : "wait") : undefined}>
          <Stat label={t("v3.online.needsYou")} value={data ? attention.length : "—"} foot={t("v3.online.needsYouFoot")} />
        </Panel>
      </div>

      {attention.length > 0 && (
        <Panel title={t("v3.online.queue")} signal={attention.some((r) => attentionOf(r) === "failed") ? "act" : "wait"} flush
          end={<span className="mono">{attention.length}</span>}>
          <table className="v3-table">
            <tbody>
              {attention.map((row) => {
                const why = attentionOf(row);
                return (
                  <tr key={row.config_id} className="click" onClick={() => open(row)}>
                    <td style={{ width: 30 }}><Lamp s={configSignal(row)} live={why === "settling"} /></td>
                    <td>
                      <div className="v3-name"><div><b>{configName(row)}</b><small>{agentLabel(row)}</small></div></div>
                    </td>
                    <td><Chip s={why === "failed" ? "act" : "wait"}>{t(`v3.online.why.${why}`)}</Chip></td>
                    <td style={{ color: "var(--v3-text-2)" }}>
                      {why === "failed" ? row.failure_reason || row.status : why === "duplicate" ? t("v2.online.duplicateWarn") : row.status}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Panel>
      )}

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters
          value={state}
          onChange={setState}
          options={[
            { value: "all", label: t("v3.online.all"), count: configs.length },
            { value: "ok", label: t("v2.online.exec.on"), s: "ok", count: count("ok") },
            { value: "off", label: t("v2.online.exec.off"), s: "off", count: count("off") },
            { value: "wait", label: t("v3.online.settling"), s: "wait", count: count("wait") },
            { value: "act", label: t("v3.online.failed"), s: "act", count: count("act") },
          ]}
        />
        <span className="v3-onl-sep" aria-hidden="true" />
        <Filters
          value={mode}
          onChange={setMode}
          options={[
            { value: "all", label: t("v3.online.anyMode") },
            { value: "scores", label: t("v2.online.mode.scores") },
            { value: "insights", label: t("v2.online.mode.insights") },
          ]}
        />
        <span className="v3-onl-sep" aria-hidden="true" />
        <Filters
          value={owner}
          onChange={setOwner}
          options={[
            { value: "all", label: t("v3.online.anyOwner") },
            ...(["agent", "experiment", "external"] as const).map((o) => ({ value: o, label: t(`v2.online.owner.${o}`) })),
          ]}
        />
        {agents.length > 1 && (
          <Select className="v3-select" style={{ width: 200 }} value={agent} onChange={(e) => setAgent(e.target.value)} aria-label={t("v3.online.agent")}>
            <option value="">{t("v3.online.anyAgent")}</option>
            {agents.map((a) => <option key={a} value={a}>{a}</option>)}
          </Select>
        )}
        <div style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center", flex: "1 1 240px", maxWidth: 320 }}>
          <Btn kind="ghost" onClick={reload} title={t("v3.tasks.refresh")} aria-label={t("v3.tasks.refresh")}><RefreshCw size={14} /></Btn>
          <div style={{ position: "relative", flex: 1 }}>
          <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
          <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => setQ(e.target.value)}
            placeholder={t("v2.online.search")} aria-label={t("v2.online.search")} />
          </div>
        </div>
      </div>

      <Panel flush>
        {loading && !data ? (
          <div style={{ padding: 20 }}><Skeleton rows={5} /></div>
        ) : error ? (
          <div style={{ padding: 20 }}><Notice s="act">{error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={configs.length ? t("v2.online.noMatch") : t("v2.online.empty")}>
            {!configs.length && t("v3.online.emptySub")}
          </Empty>
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v3.online.config")}</th>
                <th>{t("v3.online.agent")}</th>
                <th>{t("v3.online.judges")}</th>
                <th className="num">{t("v3.online.sampling")}</th>
                <th className="num">{t("v3.online.updated")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const s = configSignal(row);
                const locked = action.busy || isTransient(row);
                return (
                  <tr key={row.config_id} className="click" onClick={() => open(row)}>
                    <td style={{ width: 30 }}><Lamp s={s} live={s === "ok" || s === "wait"} title={row.status ?? undefined} /></td>
                    <td>
                      <div className="v3-name">
                        <div>
                          <b>{configName(row)}</b>
                          <small>
                            {t(`v2.online.owner.${row.owner}`)}
                            {row.status && row.status !== "ACTIVE" ? ` · ${row.status}` : ` · ${t(`v2.online.exec.${row.execution_status === "ENABLED" ? "on" : "off"}`)}`}
                          </small>
                        </div>
                      </div>
                    </td>
                    <td style={{ color: "var(--v3-text-2)" }}>{agentLabel(row)}</td>
                    <td>
                      <span className="v3-onl-judges">
                        <Chip s={modeOf(row) === "insights" ? "info" : undefined}>{t(`v2.online.mode.${modeOf(row)}`)}</Chip>
                        <span>{judges(row)}</span>
                      </span>
                    </td>
                    <td className="num">{row.sampling_percentage != null ? `${row.sampling_percentage}%` : "—"}</td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(row.updated_at ?? row.created_at)}</td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }} onClick={(e) => e.stopPropagation()}>
                      <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
                        {isEditable(row) && (
                          <Link className="v3-btn sm ghost" aria-disabled={locked || !mayRun}
                            to={locked || !mayRun ? "#" : onlineHref({ view: "edit", id: row.config_id })}
                            onClick={(e) => (locked || !mayRun) && e.preventDefault()}>
                            {t("v3.online.edit")}
                          </Link>
                        )}
                        {canToggle(row) && row.execution_status === "ENABLED" && (
                          <Btn size="sm" kind="ghost" busy={action.busy} disabled={isTransient(row)} disabledReason={t("story.reason.settling")}
                            title={t("v2.tasks.pause")} aria-label={t("v2.tasks.pause")} onClick={() => action.ask({ row, action: "pause" })}>
                            <Pause size={13} />
                          </Btn>
                        )}
                        {canToggle(row) && row.execution_status === "DISABLED" && (
                          <Btn size="sm" kind="ghost" busy={action.busy} disabled={isTransient(row) || !mayRun}
                            disabledReason={!mayRun ? permReason(t, "eval.run") : t("story.reason.settling")}
                            title={t("v2.tasks.resume")} aria-label={t("v2.tasks.resume")} onClick={() => action.ask({ row, action: "resume" })}>
                            <Play size={13} />
                          </Btn>
                        )}
                        {canToggle(row) && (
                          <Btn size="sm" kind="ghost" busy={action.busy} disabled={row.status === "DELETING" || !mayRun}
                            disabledReason={!mayRun ? permReason(t, "eval.run") : t("story.reason.deleting")}
                            title={t("v2.common.delete")} aria-label={t("v2.common.delete")}
                            onClick={() => action.ask({ row, action: "delete" })}>
                            <Trash2 size={13} />
                          </Btn>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
      {action.dialog}
    </div>
  );
}
