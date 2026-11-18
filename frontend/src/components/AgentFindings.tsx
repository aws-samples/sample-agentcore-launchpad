import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation } from "react-router-dom";

import { api } from "../lib/api";
import type { AgentFinding, FailureFinding, FindingSample } from "../lib/agent-findings";
import { useLoad } from "../v2/hooks";
import "./agent-findings.css";

function LoadProblem({ message, retry }: { message: string; retry: () => void }) {
  const { t } = useTranslation();
  return <p role="alert">{message} <button type="button" onClick={retry}>{t("agentFindings.retry")}</button></p>;
}

function Samples({ samples }: { samples?: FindingSample[] }) {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  const base = pathname.startsWith("/v3") ? "/v3" : "/v2";
  if (!samples?.length) return null;
  return <details className="af-samples"><summary>{t("agentFindings.samples", { count: samples.length })}</summary>
    <ul>{samples.map((s, i) => <li key={s.sessionId || i}>
      <Link to={`${base}/observability?tab=sessions&view=session&id=${encodeURIComponent(s.sessionId)}&range=7d`}>{s.sessionId}</Link>
      {s.findings?.map((f, j) => <p key={j}>{f.description || f.category} {f.targetId && (
        f.targetType === "TRACE_ID"
          ? <Link to={`${base}/observability?tab=traces&view=trace&id=${encodeURIComponent(f.targetId)}&range=7d`}>{f.targetId}</Link>
          : <code>{f.targetId}</code>
      )}</p>)}
    </li>)}</ul>
  </details>;
}

function FindingBody({ finding }: { finding: FailureFinding }) {
  const { t } = useTranslation();
  return <div className="af-body">
    {finding.description && <p>{finding.description}</p>}
    {finding.rootCauses?.map((cause, i) => <div className="af-cause" key={i}>
      <strong>{cause.name || t("agentFindings.cause")}</strong>
      {cause.explanation && <p>{cause.explanation}</p>}
      {cause.suggestedFix && <p><b>{t("agentFindings.fix")}</b> {cause.suggestedFix}</p>}
      <Samples samples={cause.sampleSessions} />
    </div>)}
    <Samples samples={finding.sampleSessions} />
  </div>;
}

export function BatchFindingResults({ runId, status }: { runId: string; status?: string }) {
  const { t } = useTranslation();
  const { data, error, loading, reload } = useLoad(() => api.batchFindings(runId), `batch-findings:${runId}:${status ?? ""}`);
  return <section className="agent-findings af-box" aria-label={t("agentFindings.batch")} data-testid="batch-findings">
    <h3>{t("agentFindings.batch")}</h3>
    {loading ? <p role="status">{t("agentFindings.loading")}</p> : error ? <LoadProblem message={error} retry={reload} /> : data && <>
      {data.state !== "ready" && <p role="status">{t(`agentFindings.batchState.${data.state}`)} {data.aws_status}</p>}
      {data.state === "ready" && data.findings.length === 0 && <p>{t("agentFindings.batchClean")}</p>}
      {data.diagnostics.map((msg, i) => <p key={i}>{msg}</p>)}
      {data.findings.map((item, i) => <details key={i} className="af-item">
        <summary>{item.name || t("agentFindings.pattern")} {item.affectedSessionCount != null && <small>{t("agentFindings.affected", { count: item.affectedSessionCount })}</small>}</summary>
        <FindingBody finding={item} />
      </details>)}
    </>}
  </section>;
}

function ProtectedBody({ finding }: { finding: AgentFinding }) {
  const { t } = useTranslation();
  return finding.customerMetadataStatus && finding.customerMetadataStatus !== "AVAILABLE"
    ? <p role="status">{t(`agentFindings.metadata.${finding.customerMetadataStatus}`)}</p>
    : <FindingBody finding={finding} />;
}

function LiveDetail({ agent, id }: { agent: string; id: string }) {
  const { t } = useTranslation();
  const { data, loading, error, reload } = useLoad(() => api.agentFinding(agent, id), `finding:${agent}:${id}`);
  if (loading) return <p role="status">{t("agentFindings.loading")}</p>;
  if (error) return <LoadProblem message={error} retry={reload} />;
  return data && <ProtectedBody finding={data} />;
}

function LivePage({ agent, cursor, next }: { agent: string; cursor: string | null; next: (s: string) => void }) {
  const { t, i18n } = useTranslation();
  const [expanded, setExpanded] = useState("");
  const { data, loading, error, reload } = useLoad(() => api.agentFindings(agent, cursor), `findings:${agent}:${cursor ?? ""}`);
  if (loading) return <p role="status">{t("agentFindings.loading")}</p>;
  if (error) return <LoadProblem message={error} retry={reload} />;
  if (!data) return null;
  return <><p><code>{data.application_name}</code></p>
    {!data.items.length && <p>{t(data.next_cursor || cursor ? "agentFindings.noPage" : "agentFindings.noLive")}</p>}
    {data.items.map((item) => <article className="af-item" key={item.agentInsightId}>
      <button className="af-toggle" type="button" aria-expanded={expanded === item.agentInsightId}
        onClick={() => setExpanded(expanded === item.agentInsightId ? "" : item.agentInsightId)}>
        {item.name && (!item.customerMetadataStatus || item.customerMetadataStatus === "AVAILABLE") ? item.name : item.agentInsightId}
        {item.affectedSessionCount != null && <small>{t("agentFindings.affected", { count: item.affectedSessionCount })}</small>}
      </button>
      {item.lastSeenAt && <p>{t("agentFindings.lastSeen", { time: new Date(item.lastSeenAt).toLocaleString(i18n.language) })}</p>}
      {expanded === item.agentInsightId && <LiveDetail key={item.agentInsightId} agent={agent} id={item.agentInsightId} />}
    </article>)}
    {data.next_cursor && <button type="button" onClick={() => next(data.next_cursor!)}>{t("agentFindings.next")}</button>}
  </>;
}

function LiveAgent({ agent }: { agent: string }) {
  const { t } = useTranslation();
  const [pages, setPages] = useState<string[]>([]);
  const cursor = pages.at(-1) ?? null;
  return <><LivePage key={cursor ?? ""} agent={agent} cursor={cursor} next={(s) => setPages([...pages, s])} />
    {pages.length > 0 && <button type="button" onClick={() => setPages(pages.slice(0, -1))}>{t("agentFindings.previous")}</button>}
  </>;
}

function LiveBrowser() {
  const { t } = useTranslation();
  const store = useLoad(() => api.insightStore(), "finding-store");
  const agents = useLoad(() => api.listAgents(), "finding-agents");
  const [agent, setAgent] = useState("");
  return <div data-testid="online-findings">
    {store.loading ? <p>{t("agentFindings.loading")}</p> : store.error ? <LoadProblem message={store.error} retry={store.reload} /> :
      store.data?.state === "ready" ? <>
        <p>{t("agentFindings.liveHint")}</p>
        {agents.error ? <LoadProblem message={agents.error} retry={agents.reload} /> : <label>{t("agentFindings.agent")}
          <select value={agent} onChange={(e) => setAgent(e.target.value)}><option value="">{t("agentFindings.pickAgent")}</option>
          {agents.data?.agents.filter((a) => a.resource_id && a.status === "active").map((a) =>
            <option key={a.id} value={a.id}>{a.display_name || a.name}</option>)}</select>
        </label>}
        {agent && <LiveAgent key={agent} agent={agent} />}
      </> : store.data && <p role="status">{t(`agentFindings.store.${store.data.state}`)} <button type="button" onClick={store.reload}>{t("agentFindings.refresh")}</button></p>}
  </div>;
}

/** Read only the active source. Online counts refer to the agent endpoint's history,
 * while the overview's time range selects batch runs. Page shells remount per project. */
export function FindingsBrowser({ runs, history, busy = false }: {
  runs: { id: string; name: string; status: string }[]; history: ReactNode; busy?: boolean;
}) {
  const { t } = useTranslation();
  const [source, setSource] = useState<"batch" | "online" | "history">("batch");
  const [run, setRun] = useState("");
  const selected = runs.find((r) => r.id === run);
  return <section className="agent-findings af-box" data-testid="findings-browser">
    <h2>{t("agentFindings.title")}</h2>
    <div className="af-switch" role="group" aria-label={t("agentFindings.title")}>
      {(["batch", "online", "history"] as const).map((key) => <button type="button" aria-pressed={source === key}
        key={key} onClick={() => setSource(key)}>{t(`agentFindings.${key}`)}</button>)}
    </div>
    {source === "online" ? <LiveBrowser /> : source === "history" ? history : <>
      <p>{t("agentFindings.batchHint")}</p>
      {runs.length > 0 ? <label>{t("agentFindings.run")}<select value={selected?.id ?? ""} onChange={(e) => setRun(e.target.value)}>
        <option value="">{t("agentFindings.pickRun")}</option>
        {runs.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
      </select></label> : <p>{t(busy ? "agentFindings.loading" : "agentFindings.noBatch")}</p>}
      {selected && <BatchFindingResults key={selected.id} runId={selected.id} status={selected.status} />}
    </>}
  </section>;
}
