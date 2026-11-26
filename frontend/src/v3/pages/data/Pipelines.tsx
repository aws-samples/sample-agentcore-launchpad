import { Pencil, Play, Plus, Search, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { ErrorCallout } from "../../../components/ErrorCallout";
import { api, type V2Pipeline } from "../../../lib/api";
import { scenarioTypeOf } from "../../../lib/pipelineAgentcore";
import { rangeLabel } from "../../../v2/format";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useFailure } from "../../failure";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Confirm, Empty, Filters, Lamp, Notice, Panel, type Signal, Skeleton } from "../../ui";
import { pipelineSignal } from "./common";

const editHref = (id: string) => `/v2/eval/data?tab=pipelines&view=pipeline&id=${encodeURIComponent(id)}`;

/** Trace → dataset pipelines: failed ones lead; run, edit (hosted editor) and delete as V2. */
export function PipelinesTab() {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const { current } = useWorkspace();
  const list = useLoad(() => api.v2Pipelines(), `v3-data-pipelines:${current?.id ?? ""}`);
  const datasets = useLoad(() => api.v2Datasets(), `v3-data-pipeline-ds:${current?.id ?? ""}`);
  const names = useMemo(() => new Map((datasets.data?.datasets ?? []).map((d) => [d.id, d.name])), [datasets.data]);
  const [state, setState] = useState<"all" | Signal>("all");
  const [q, setQ] = useState("");
  const [running, setRunning] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<V2Pipeline | null>(null);
  const [busy, setBusy] = useState(false);
  const all = useMemo(() => list.data?.pipelines ?? [], [list.data]);
  const order: Record<Signal, number> = { act: 0, wait: 1, ok: 2, info: 3, off: 4 };
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all
      .filter((p) => (state === "all" || pipelineSignal(p.status) === state) && (!needle || `${p.name} ${p.description}`.toLowerCase().includes(needle)))
      .sort((a, b) => order[pipelineSignal(a.status)] - order[pipelineSignal(b.status)]);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `order` is a constant table
  }, [all, state, q]);
  const count = (s: Signal) => all.filter((p) => pipelineSignal(p.status) === s).length;

  const run = async (p: V2Pipeline) => {
    setRunning(p.id);
    try {
      failed.clear();
      const out = await api.v2RunPipeline(p.id);
      if (out.status === "failed") failed.fail(out.last_run?.error ?? t("v2.pipelines.failed"));
      else toast("ok", t("v2.pipelines.ran", { count: out.last_run?.added ?? 0 }));
      list.reload();
    } catch (err) {
      failed.fail(err);
    } finally {
      setRunning(null);
    }
  };
  const remove = async () => {
    if (!deleting) return;
    setBusy(true);
    try {
      failed.clear();
      await api.v2DeletePipeline(deleting.id);
      toast("ok", t("v2.pipelines.deleted"));
      setDeleting(null);
      list.reload();
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(false);
    }
  };
  // what a pipeline reads, per source type
  const input = (p: V2Pipeline) => {
    const src = p.config.source;
    if (src.type === "agentcore") {
      const scenario = scenarioTypeOf(src) === "DYNAMIC" ? t("v2.pipelines.ac.dynamic") : t("v2.pipelines.ac.fixed");
      const span = src.batch_run_id ? t("v2.pipelines.ac.fromRunShort", { id: src.batch_run_id }) : rangeLabel(t, src.range);
      return [t("v2.pipelines.srcAgentcore"), src.agent ?? src.agent_id ?? "—", span, scenario].join(" · ");
    }
    if (src.type === "logs") {
      const groups = src.log_groups ?? [];
      const group = groups.length > 1 ? `${groups[0]} +${groups.length - 1}` : (groups[0] ?? "—");
      return [t("v2.pipelines.srcLogs"), group, rangeLabel(t, src.range)].join(" · ");
    }
    return [src.agent ?? t("v2.pipelines.allAgents"), rangeLabel(t, src.range), t(`v2.pipelines.status.${src.status}`)].join(" · ");
  };
  // the last run in one line; an agentcore run adds its generation counts (and the
  // AWS error codes / failure reason as the tooltip)
  const lastRun = (p: V2Pipeline) => {
    const run = p.last_run;
    if (!run) return t("v2.pipelines.never");
    const gen = run.generation;
    let text: string;
    if (p.status === "running" && gen) text = t(`v2.pipelines.ac.stage.${gen.stage}`, { defaultValue: gen.stage });
    else if (run.error) text = t("v2.pipelines.lastError");
    else if (gen)
      text = t("v2.pipelines.ac.lastRunShort", { requested: gen.requested, written: gen.results?.examplesWritten ?? 0, added: run.added });
    else text = t("v2.pipelines.lastAdded", { count: run.added });
    const codes = (gen?.error_details ?? []).map((e) => `${e.code} × ${e.count}`);
    const title = [run.error, gen?.failure_reason, ...codes].filter(Boolean).join("\n") || undefined;
    return <span title={title}>{ago(run.at)} · {text}</span>;
  };
  const output = (p: V2Pipeline) => {
    const id = p.config.output.dataset_id;
    if (id) return names.get(id) ?? id;
    return t("v2.pipelines.newDataset", { name: p.config.output.dataset_name ?? "" });
  };

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-pipelines-error" />
      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters
          value={state}
          onChange={setState}
          options={[
            { value: "all", label: t("v3.data.anyStatus"), count: all.length },
            { value: "act", label: t("v2.pipelines.state.failed"), s: "act", count: count("act") },
            { value: "wait", label: t("v2.pipelines.state.running"), s: "wait", count: count("wait") },
            { value: "ok", label: t("v2.pipelines.state.succeeded"), s: "ok", count: count("ok") },
            { value: "off", label: t("v2.pipelines.state.idle"), s: "off", count: count("off") },
          ]}
        />
        <div style={{ marginLeft: "auto", display: "flex", gap: 10, alignItems: "center" }}>
          <div style={{ position: "relative", width: 240 }}>
            <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
            <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => setQ(e.target.value)}
              placeholder={t("v2.pipelines.search")} aria-label={t("v2.pipelines.search")} />
          </div>
          <Link to="/v2/eval/data?tab=pipelines&view=pipeline-new" className="v3-btn primary"><Plus size={14} /> {t("v2.pipelines.new")}</Link>
        </div>
      </div>
      <p style={{ margin: 0, color: "var(--v3-text-3)", fontSize: 13 }}>{t("v2.pipelines.intro")}</p>
      <Panel flush>
        {list.loading && !list.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : list.error ? (
          <div style={{ padding: 20 }}><Notice s="act"><ApiErrorDetail error={list.failure ?? list.error} /></Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t("v2.pipelines.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v2.pipelines.colName")}</th>
                <th>{t("v2.pipelines.colInput")}</th>
                <th>{t("v2.pipelines.colOutput")}</th>
                <th>{t("v2.pipelines.colStatus")}</th>
                <th className="num">{t("v2.pipelines.colLastRun")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => {
                const s = pipelineSignal(p.status);
                return (
                  <tr key={p.id}>
                    <td style={{ width: 24 }}><Lamp s={s} live={s === "wait"} /></td>
                    <td><div className="v3-name"><div><b>{p.name}</b><small>{p.description || p.id}</small></div></div></td>
                    <td style={{ color: "var(--v3-text-2)" }}>{input(p)}</td>
                    <td>{output(p)}</td>
                    <td><Chip s={s === "off" ? undefined : s}>{t(`v2.pipelines.state.${p.status}`)}</Chip></td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{lastRun(p)}</td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }}>
                      <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
                        <Btn size="sm" disabled={running !== null} onClick={() => void run(p)}>
                          <Play size={13} /> {running === p.id ? t("v2.pipelines.running") : t("v2.pipelines.run")}
                        </Btn>
                        <Link to={editHref(p.id)} className="v3-btn ghost sm" title={t("v3.data.edit")}><Pencil size={13} /></Link>
                        <Btn size="sm" kind="ghost" onClick={() => setDeleting(p)} title={t("v3.data.delete")}><Trash2 size={13} /></Btn>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
      {deleting && (
        <Confirm title={t("v2.pipelines.deleteTitle")} confirmLabel={t("v3.data.delete")} cancelLabel={t("v3.common.cancel")} danger busy={busy}
          onCancel={() => setDeleting(null)} onConfirm={() => void remove()}>
          {t("v2.pipelines.deleteBody", { name: deleting.name })}
        </Confirm>
      )}
    </div>
  );
}
