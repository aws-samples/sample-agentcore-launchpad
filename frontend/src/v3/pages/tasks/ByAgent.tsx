import { RefreshCw, RotateCcw, Search, X } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useSearchParams } from "react-router-dom";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { useAuth } from "../../../auth/auth-context";
import { methodLabel } from "../../../components/methodChipMeta";
import { api, type ScorecardRun } from "../../../lib/api";
import { evaluatorLabel } from "../../../lib/evaluators";
import { deltaTone } from "../../../v2/compare";
import { fmtScore } from "../../../v2/format";
import { dismissKey } from "../../../v2/attentionDismiss";
import { useAttentionDismissals } from "../../../v2/hooks";
import { backState } from "../../../v2/pages/tasks/back";
import { RerunDialog } from "../../../v2/pages/tasks/RerunDialog";
import { SCORECARD_ATTENTION_CAP, scorecardAttention, type ScorecardEntry } from "../../../v2/pages/tasks/scorecardAttention";
import { benchName, rerunBlockText, runName, versionHint, versionShort } from "../../../v2/pages/tasks/scorecardText";
import {
  type AgentRow,
  agentMethods,
  agentRows,
  type AgentStatusFilter,
  type ChangeKind,
  filterAgentRows,
  rerunSource,
  shortDate,
  shortTime,
} from "../../../v2/scorecard";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useLoad, useToast } from "../../hooks";
import { permReason } from "../../reasons";
import { Select } from "../../select";
import { Btn, Chip, Empty, Lamp, Notice, Panel, Skeleton, Spark } from "../../ui";
import { AtShell, HostedV2, ScoreChip } from "./common";

const PAGE = 25;
/** `eval.agents`: the same browser-local dismissals as V2's 按 Agent panel. */
const DISMISS_SCOPE = "eval.agents";

const when = (value: string | null | undefined) => `${shortDate(value)} ${shortTime(value)}`.trim();

/** ▲ / ▼ and the size (never colour alone), "≈ flat" inside ±0.02 — V2's DeltaChip on V3. */
function Delta({ value }: { value: number }) {
  const { t } = useTranslation();
  const tone = deltaTone(value);
  if (tone === "flat") return <Chip title={t("v2.compare.flatHint")}>{t("v2.compare.flat")}</Chip>;
  return <Chip s={tone === "up" ? "ok" : "act"}>{`${tone === "up" ? "▲" : "▼"} ${Math.abs(value).toFixed(2)}`}</Chip>;
}

/**
 * 按 Agent (the 评估任务 default tab): one row per evaluated agent — its main benchmark,
 * latest composite, the change against the previous version (or why there is none), a
 * trend line, counts and 再评一次 — under a 需要关注 panel of regressions and failed runs.
 * V2's `AgentScoreList` on V3's kit: the same ledger-only scorecard read and pure rules
 * (`v2/scorecard.ts`, `scorecardAttention.ts`); a row opens the agent's page.
 */
export function ByAgent() {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const location = useLocation();
  const { can } = useAuth();
  const { current } = useWorkspace();
  const sc = useLoad(() => api.evaluationScorecard(), `v3-scorecard:${current?.id ?? ""}`);
  const [q, setQ] = useState("");
  // deleted agents keep their runs on the ledger; they stay out of the list unless asked for
  const [status, setStatus] = useState<AgentStatusFilter>("active");
  const [method, setMethod] = useState("");
  const [change, setChange] = useState<"" | ChangeKind>("");
  const [shown, setShown] = useState(PAGE);
  const [rerun, setRerun] = useState<{ row: AgentRow; run: ScorecardRun } | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const { dismissed, dismiss, restore } = useAttentionDismissals(DISMISS_SCOPE);
  const toast = useToast();
  const mayRun = can("eval.run");

  const rows = useMemo(() => (sc.data ? agentRows(sc.data) : []), [sc.data]);
  const { filtered, hiddenDeleted } = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const matches = (list: AgentRow[]) =>
      needle
        ? list.filter((row) => `${row.agent.name} ${row.agent.key} ${row.main ? benchName(t, row.main) : ""}`.toLowerCase().includes(needle))
        : list;
    return {
      filtered: matches(filterAgentRows(rows, { status, method, change })),
      // what 已隐藏 N would add back: deleted rows that pass every other filter
      hiddenDeleted: status === "active" ? matches(filterAgentRows(rows, { status: "deleted", method, change })).length : 0,
    };
  }, [rows, q, t, status, method, change]);
  const methods = useMemo(() => agentMethods(rows), [rows]);
  const entries = useMemo(() => (sc.data ? scorecardAttention(sc.data) : []), [sc.data]);
  // V2's stored identity (`key|signal`): the same store as V2's panel, and a row whose
  // signal changes shows again
  const attention = entries.filter((entry) => !dismissed.has(dismissKey(entry)));
  const hidden = entries.filter((entry) => dismissed.has(dismissKey(entry)));
  const act = attention.some((entry) => entry.signal === "act");

  const openAgent = (key: string, bench?: string) =>
    setParams(bench ? { view: "agent", key, bench } : { view: "agent", key }, { state: backState(location.search) });
  const openCompare = (a: string, b: string) => setParams({ view: "compare", a, b }, { state: backState(location.search) });
  const openRun = (id: string) => setParams({ view: "detail", kind: "run", id }, { state: backState(location.search) });

  const attentionRow = (entry: ScorecardEntry) => {
    if (entry.kind === "failed") {
      return {
        title: t("v2.scorecard.attention.failed", { agent: entry.agent?.name ?? entry.run.agent_key, name: runName(entry.run) }),
        hint: entry.detail || t("v2.taskStatus.failed"),
        at: entry.run.created_at,
        open: () => openRun(entry.run.id),
      };
    }
    const lead = entry.worst[0];
    const title = entry.compositeDrop
      ? t("v2.scorecard.attention.regressionComposite", {
          agent: entry.agent.name, to: entry.to, from: entry.from, by: Math.abs(entry.composite.delta ?? 0).toFixed(2),
        })
      : t("v2.scorecard.attention.regressionEvaluator", {
          agent: entry.agent.name, to: entry.to, from: entry.from,
          evaluator: lead ? evaluatorLabel(t, lead.evaluatorId) : "", by: Math.abs(lead?.delta ?? 0).toFixed(2),
        });
    const changes = entry.worst
      .map((c) => t("v2.scorecard.attention.change", { name: evaluatorLabel(t, c.evaluatorId), a: fmtScore(c.a), b: fmtScore(c.b) }))
      .join(t("v2.scorecard.listSep"));
    const hint = [entry.kind === "pending" ? t("v2.scorecard.attention.pending") : null, benchName(t, entry.bench), changes]
      .filter(Boolean)
      .join(" · ");
    return { title, hint, at: entry.latest.created_at, open: () => openCompare(entry.previous.id, entry.latest.id) };
  };

  return (
    <div className="v3-reveal v3-task v3-by-agent" style={{ display: "grid", gap: 16 }} data-testid="v3-by-agent">
      {sc.data && (attention.length > 0 || hidden.length > 0) && (
        <Panel title={t("v2.scorecard.attention.title")} signal={act ? "act" : attention.length ? "wait" : undefined} flush end={<span className="mono">{attention.length}</span>}>
          <p style={{ margin: "0 20px 8px", color: "var(--v3-text-3)", fontSize: 13 }}>{t("v2.scorecard.attention.sub")}</p>
          <table className="v3-table">
            <tbody>
              {attention.slice(0, SCORECARD_ATTENTION_CAP).map((entry) => {
                const row = attentionRow(entry);
                return (
                  <tr key={entry.key} className="click" onClick={row.open}>
                    <td style={{ width: 30 }}><Lamp s={entry.signal} /></td>
                    <td>
                      <div className="v3-name">
                        <div>
                          <b>{row.title}</b>
                          <small style={{ color: entry.signal === "act" ? "var(--v3-act)" : undefined }}>{row.hint}</small>
                        </div>
                      </div>
                    </td>
                    <td className="num" style={{ color: "var(--v3-text-3)", whiteSpace: "nowrap" }}>{when(row.at)}</td>
                    <td style={{ width: 1 }} onClick={(e) => e.stopPropagation()}>
                      <Btn size="sm" kind="ghost" title={t("v2.attention.dismiss")} aria-label={t("v2.attention.dismiss")}
                        onClick={() => dismiss(dismissKey(entry))}>
                        <X size={13} />
                      </Btn>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {hidden.length > 0 && (
            <div style={{ padding: "8px 20px 14px", display: "grid", gap: 6 }}>
              <div>
                <button type="button" className="v3-btn ghost sm" aria-expanded={showHidden} onClick={() => setShowHidden((v) => !v)}
                  title={t("v2.attention.localHint")}>
                  {t("v2.attention.dismissedCount", { count: hidden.length })}
                </button>
              </div>
              {showHidden && hidden.map((entry) => (
                <div key={entry.key} style={{ display: "flex", gap: 10, alignItems: "center", color: "var(--v3-text-3)", fontSize: 13 }}>
                  <Lamp s={entry.signal} />
                  <span style={{ flex: 1, minWidth: 0 }} className="v3-task-clip">{attentionRow(entry).title}</span>
                  <button type="button" className="v3-btn ghost sm" onClick={() => restore(dismissKey(entry))}>{t("v2.attention.restore")}</button>
                </div>
              ))}
            </div>
          )}
        </Panel>
      )}
      {sc.data?.truncated && <Notice>{t("v2.scorecard.truncated")}</Notice>}

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Select className="v3-select" style={{ width: 150 }} value={status} aria-label={t("v2.scorecard.filter.status")}
          onChange={(e) => { setStatus(e.target.value as AgentStatusFilter); setShown(PAGE); }}>
          <option value="active">{t("v2.scorecard.filter.statusActive")}</option>
          <option value="deleted">{t("v2.scorecard.deleted")}</option>
          <option value="">{`${t("v2.scorecard.filter.status")}: ${t("v2.common.all")}`}</option>
        </Select>
        <Select className="v3-select" style={{ width: 170 }} value={method} aria-label={t("v2.scorecard.filter.method")}
          onChange={(e) => { setMethod(e.target.value); setShown(PAGE); }}>
          <option value="">{`${t("v2.scorecard.filter.method")}: ${t("v2.common.all")}`}</option>
          {methods.map((m) => <option key={m} value={m}>{m === "logs" ? t("v2.tasks.cw.short") : methodLabel(m)}</option>)}
        </Select>
        <Select className="v3-select" style={{ width: 170 }} value={change} aria-label={t("v2.scorecard.col.vsPrev")}
          onChange={(e) => { setChange(e.target.value as "" | ChangeKind); setShown(PAGE); }}>
          <option value="">{`${t("v2.scorecard.col.vsPrev")}: ${t("v2.common.all")}`}</option>
          {(["down", "up", "minor", "none"] as const).map((k) => <option key={k} value={k}>{t(`v2.scorecard.filter.change.${k}`)}</option>)}
        </Select>
        {hiddenDeleted > 0 && (
          <button type="button" className="v3-btn ghost sm" onClick={() => setStatus("")}>
            {t("v2.scorecard.filter.hiddenDeleted", { count: hiddenDeleted })}
          </button>
        )}
        <div style={{ marginLeft: "auto", display: "flex", gap: 8, alignItems: "center", flex: "1 1 240px", maxWidth: 320 }}>
          <Btn kind="ghost" onClick={sc.reload} title={t("v3.tasks.refresh")} aria-label={t("v3.tasks.refresh")}><RefreshCw size={14} /></Btn>
          <div style={{ position: "relative", flex: 1 }}>
            <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
            <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => setQ(e.target.value)}
              placeholder={t("v2.scorecard.search")} aria-label={t("v2.scorecard.search")} />
          </div>
        </div>
      </div>

      <Panel flush end={sc.data ? <span>{t("v2.scorecard.agentCount", { count: filtered.length })}</span> : undefined}>
        {sc.loading && !sc.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={6} /></div>
        ) : sc.error && !sc.data ? (
          <div style={{ padding: 20 }}><Notice s="act"><ApiErrorDetail error={sc.failure ?? sc.error} /></Notice></div>
        ) : filtered.length === 0 ? (
          <Empty title={rows.length ? t("v2.scorecard.emptyFiltered") : t("v2.scorecard.empty")} />
        ) : (
          <>
            <div className="v3-table-scroll">
            <table className="v3-table" data-testid="v3-by-agent-table">
              <thead>
                <tr>
                  <th>{t("v2.scorecard.col.agent")}</th>
                  <th title={t("v2.scorecard.listHint")}>{t("v2.scorecard.col.main")}</th>
                  <th>{t("v2.scorecard.col.latest")}</th>
                  <th>{t("v2.scorecard.col.vsPrev")}</th>
                  <th>{t("v2.scorecard.col.trend")}</th>
                  <th>{t("v2.scorecard.col.evals")}</th>
                  <th className="num">{t("v2.scorecard.col.lastEval")}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {filtered.slice(0, shown).map((row) => {
                  const { agent, head } = row;
                  const meta = agent.log_source
                    ? t("v2.tasks.cw.short")
                    : [agent.method ? methodLabel(agent.method) : null, agent.current_seq != null ? t("v2.scorecard.current", { seq: agent.current_seq }) : null]
                        .filter(Boolean)
                        .join(" · ");
                  const size = row.main?.runs[row.main.runs.length - 1]?.benchmark.size;
                  const target = rerunSource(row.main, agent);
                  const blocked = rerunBlockText(t, target.block, mayRun);
                  const reason = head.reason ?? "noCompleted";
                  return (
                    <tr key={agent.key} className="click" onClick={() => openAgent(agent.key)} data-testid={`v3-by-agent-row-${agent.key}`}>
                      <td>
                        <div className="v3-name">
                          <div>
                            <b className={agent.log_source ? "mono v3-task-clip" : "v3-task-clip"} title={agent.name}>{agent.name}</b>
                            <small>
                              {meta}
                              {agent.deleted && <> · {t("v2.scorecard.deleted")}</>}
                            </small>
                          </div>
                        </div>
                      </td>
                      <td>
                        {row.main ? (
                          <>
                            <span className="v3-task-clip" title={benchName(t, row.main)}>{benchName(t, row.main)}</span>
                            <small className="v3-task-sub">
                              {t(`v2.scorecard.benchKind.${row.main.kind}`)}
                              {size != null && row.main.kind !== "live" && ` · ${t("v2.scorecard.benchSize", { count: size })}`}
                            </small>
                          </>
                        ) : (
                          <span style={{ color: "var(--v3-text-3)" }}>—</span>
                        )}
                      </td>
                      <td style={{ whiteSpace: "nowrap" }}>
                        <ScoreChip value={head.score} />
                        {head.latest && (
                          <small className="v3-task-sub" title={versionHint(t, head.latest.snapshot)}>
                            {[versionShort(t, head.latest.snapshot), shortDate(head.latest.created_at)].filter(Boolean).join(" · ")}
                          </small>
                        )}
                      </td>
                      <td>
                        {head.delta == null || !head.previous ? (
                          // a short label keeps the row at two lines; the full sentence is the tooltip
                          <span style={{ color: "var(--v3-text-3)" }} title={t(`v2.scorecard.reason.${reason}`)}>
                            {t(`v2.scorecard.reasonShort.${reason}`)}
                          </span>
                        ) : (
                          <>
                            <Delta value={head.delta} />
                            <small className="v3-task-sub">
                              {head.byVersion && head.previous.snapshot
                                ? t("v2.scorecard.vs.version", { seq: head.previous.snapshot.seq })
                                : t("v2.scorecard.vs.run")}
                            </small>
                          </>
                        )}
                      </td>
                      <td title={t("v2.scorecard.sparkLabel", { values: row.spark.map((v) => fmtScore(v)).join(t("v2.compare.sep")) })}>
                        {row.spark.length >= 2 ? <Spark values={row.spark} s={head.signal === "act" ? "act" : "ok"} /> : <span style={{ color: "var(--v3-text-3)" }}>—</span>}
                      </td>
                      <td>
                        {t("v2.scorecard.counts.runs", { count: row.runs.length })}
                        {/* the two counts wrap at the separator, never inside one */}
                        <small className="v3-task-sub">
                          <span style={{ whiteSpace: "nowrap" }}>
                            {row.versions ? t("v2.scorecard.counts.versions", { count: row.versions }) : t("v2.scorecard.noVersion")} ·
                          </span>{" "}
                          <span style={{ whiteSpace: "nowrap" }}>{t("v2.scorecard.counts.benches", { count: row.benches.length })}</span>
                        </small>
                      </td>
                      <td className="num" style={{ color: "var(--v3-text-3)", whiteSpace: "nowrap" }}>
                        {when(row.newest?.created_at)}
                        <small className="v3-task-sub" title={t("v2.common.creator")}>{row.newest?.created_by_name || "—"}</small>
                      </td>
                      <td style={{ width: 1, whiteSpace: "nowrap" }} onClick={(e) => e.stopPropagation()}>
                        <Btn size="sm" kind="ghost" disabled={!!blocked || !target.run}
                          disabledReason={blocked ?? permReason(t, "eval.run")}
                          title={t("v2.scorecard.rerun.action")} aria-label={t("v2.scorecard.rerun.action")}
                          onClick={() => target.run && setRerun({ row, run: target.run })}>
                          <RotateCcw size={13} />
                        </Btn>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            </div>
            {filtered.length > shown && (
              <div style={{ padding: 14, display: "flex", justifyContent: "center" }}>
                <Btn size="sm" onClick={() => setShown((n) => n + PAGE)}>{t("v3.tasks.more", { count: filtered.length - shown })}</Btn>
              </div>
            )}
          </>
        )}
      </Panel>

      {rerun && (
        <AtShell>
          <HostedV2>
            <RerunDialog
              key={rerun.run.id}
              agent={rerun.row.agent}
              source={rerun.run}
              onClose={() => setRerun(null)}
              onStarted={(started) => {
                const { row, run } = rerun;
                // the dialog's own V2 toast unmounts with it: say it on the V3 channel
                toast("ok", t("v2.scorecard.rerun.started", { name: started.name ?? "" }));
                setRerun(null);
                // the new run is the next point of that benchmark's line
                openAgent(row.agent.key, run.benchmark.key);
              }}
            />
          </HostedV2>
        </AtShell>
      )}
    </div>
  );
}
