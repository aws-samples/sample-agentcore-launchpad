import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation } from "react-router-dom";

import { api } from "../lib/api";
import type { AgentFinding, FailureFinding, FindingSample } from "../lib/agent-findings";
import type { FindingsSkin, NoteTone } from "../lib/findings-skin";
import { v2FindingsSkin } from "../v2/findingsSkin";
import { useLoad } from "../v2/hooks";
import { v3FindingsSkin } from "../v3/findingsSkin";
import "./agent-findings.css";

/** The console the panel sits in decides the primitives it is drawn with. */
function useConsole(): { skin: FindingsSkin; base: "/v2" | "/v3" } {
  const { pathname } = useLocation();
  return pathname.startsWith("/v3") ? { skin: v3FindingsSkin, base: "/v3" } : { skin: v2FindingsSkin, base: "/v2" };
}

function LoadProblem({ skin, message, retry }: { skin: FindingsSkin; message: string; retry: () => void }) {
  const { t } = useTranslation();
  return <skin.Note tone="error" action={<skin.Button onClick={retry}>{t("agentFindings.retry")}</skin.Button>}>{message}</skin.Note>;
}

function Samples({ samples, base }: { samples?: FindingSample[]; base: string }) {
  const { t } = useTranslation();
  if (!samples?.length) return null;
  return (
    <details className="fnd-samples">
      <summary>{t("agentFindings.samples", { count: samples.length })}</summary>
      <ul>
        {samples.map((s, i) => (
          <li key={s.sessionId || i}>
            <Link className="fnd-session" to={`${base}/observability?tab=sessions&view=session&id=${encodeURIComponent(s.sessionId)}&range=7d`}>{s.sessionId}</Link>
            {s.findings?.map((f, j) => (
              <p key={j}>
                {f.description || f.category}{" "}
                {f.targetId && (f.targetType === "TRACE_ID"
                  ? <Link className="fnd-session" to={`${base}/observability?tab=traces&view=trace&id=${encodeURIComponent(f.targetId)}&range=7d`}>{f.targetId}</Link>
                  : <code>{f.targetId}</code>)}
              </p>
            ))}
          </li>
        ))}
      </ul>
    </details>
  );
}

function FindingBody({ finding, base }: { finding: FailureFinding; base: string }) {
  const { t } = useTranslation();
  return (
    <div className="fnd-body">
      {finding.description && <p className="fnd-desc">{finding.description}</p>}
      {finding.rootCauses?.map((cause, i) => (
        <div className="fnd-cause" key={i}>
          <div className="fnd-cause-head">
            <span className="fnd-label">{t("agentFindings.cause")}</span>
            <strong>{cause.name || t("agentFindings.cause")}</strong>
            {cause.affectedSessionCount != null && <small>{t("agentFindings.affected", { count: cause.affectedSessionCount })}</small>}
          </div>
          {cause.explanation && <p>{cause.explanation}</p>}
          {cause.suggestedFix && <p className="fnd-fix"><span className="fnd-label">{t("agentFindings.fix")}</span>{cause.suggestedFix}</p>}
          <Samples samples={cause.sampleSessions} base={base} />
        </div>
      ))}
      <Samples samples={finding.sampleSessions} base={base} />
    </div>
  );
}

/** Rank, name and a bar for the share of the largest pattern — the header every pattern shares. */
function Head({ skin, rank, title, count, max, extra }: {
  skin: FindingsSkin; rank: number; title: string; count?: number; max: number; extra?: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <>
      <span className="fnd-rank">{rank}</span>
      <span className="fnd-title"><b>{title}</b>{extra}</span>
      {count != null && (
        <span className="fnd-meter">
          <span className="fnd-bar"><i style={{ width: `${Math.max(4, Math.round((count / Math.max(1, max)) * 100))}%` }} /></span>
          <skin.Tag tone="red">{t("agentFindings.affected", { count })}</skin.Tag>
        </span>
      )}
    </>
  );
}

const BATCH_TONE: Record<string, NoteTone> = {
  pending: "info", partial: "warn", unavailable: "warn", not_applicable: "info", error: "error",
};

function BatchBody({ runId, status, skin, base }: { runId: string; status?: string; skin: FindingsSkin; base: string }) {
  const { t } = useTranslation();
  const { data, error, loading, reload } = useLoad(() => api.batchFindings(runId), `batch-findings:${runId}:${status ?? ""}`);
  if (loading) return <skin.Busy />;
  if (error) return <LoadProblem skin={skin} message={error} retry={reload} />;
  if (!data) return null;
  const max = Math.max(1, ...data.findings.map((f) => f.affectedSessionCount ?? 0));
  return (
    <div className="fnd-stack" data-testid="batch-findings">
      {data.state !== "ready" && <skin.Note tone={BATCH_TONE[data.state] ?? "info"}>{t(`agentFindings.batchState.${data.state}`)} {data.aws_status}</skin.Note>}
      {data.state === "ready" && data.findings.length === 0 && <skin.Note tone="info">{t("agentFindings.batchClean")}</skin.Note>}
      {data.diagnostics.map((msg, i) => <p className="fnd-diag" key={i}>{msg}</p>)}
      {data.findings.map((item, i) => (
        <details key={i} className="fnd-item" open={i === 0}>
          <summary className="fnd-head"><Head skin={skin} rank={i + 1} title={item.name || t("agentFindings.pattern")} count={item.affectedSessionCount} max={max} /></summary>
          <FindingBody finding={item} base={base} />
        </details>
      ))}
    </div>
  );
}

/** One evaluation run's Failure Analysis snapshot. `bare` skips the card when a page already provides one. */
export function BatchFindingResults({ runId, status, bare = false }: { runId: string; status?: string; bare?: boolean }) {
  const { t } = useTranslation();
  const { skin, base } = useConsole();
  const body = <BatchBody runId={runId} status={status} skin={skin} base={base} />;
  return bare ? body : <skin.Section title={t("agentFindings.batch")} testId="batch-findings-card">{body}</skin.Section>;
}

function ProtectedBody({ finding, skin, base }: { finding: AgentFinding; skin: FindingsSkin; base: string }) {
  const { t } = useTranslation();
  return finding.customerMetadataStatus && finding.customerMetadataStatus !== "AVAILABLE"
    ? <skin.Note tone="warn">{t(`agentFindings.metadata.${finding.customerMetadataStatus}`)}</skin.Note>
    : <FindingBody finding={finding} base={base} />;
}

function LiveDetail({ agent, id, skin, base }: { agent: string; id: string; skin: FindingsSkin; base: string }) {
  const { data, loading, error, reload } = useLoad(() => api.agentFinding(agent, id), `finding:${agent}:${id}`);
  if (loading) return <skin.Busy />;
  if (error) return <LoadProblem skin={skin} message={error} retry={reload} />;
  return data && <ProtectedBody finding={data} skin={skin} base={base} />;
}

function LivePage({ agent, cursor, next, skin, base, emptyKey }: {
  agent: string; cursor: string | null; next: (s: string) => void; skin: FindingsSkin; base: string; emptyKey: string;
}) {
  const { t, i18n } = useTranslation();
  const [expanded, setExpanded] = useState("");
  const { data, loading, error, reload } = useLoad(() => api.agentFindings(agent, cursor), `findings:${agent}:${cursor ?? ""}`);
  if (loading) return <skin.Busy />;
  if (error) return <LoadProblem skin={skin} message={error} retry={reload} />;
  if (!data) return null;
  const max = Math.max(1, ...data.items.map((f) => f.affectedSessionCount ?? 0));
  return (
    <div className="fnd-stack">
      <p className="fnd-meta-row">
        <code>{data.application_name}</code>
        <skin.Button onClick={() => { setExpanded(""); reload(); }}>{t("agentFindings.refresh")}</skin.Button>
      </p>
      {!data.items.length && <skin.Note tone="info">{t(data.next_cursor || cursor ? "agentFindings.noPage" : emptyKey)}</skin.Note>}
      {data.items.map((item, i) => {
        const open = expanded === item.agentInsightId;
        const readable = !item.customerMetadataStatus || item.customerMetadataStatus === "AVAILABLE";
        return (
          <article className="fnd-item" key={item.agentInsightId} data-open={open || undefined}>
            <button className="fnd-head" type="button" aria-expanded={open} onClick={() => setExpanded(open ? "" : item.agentInsightId)}>
              <Head skin={skin} rank={i + 1} title={item.name && readable ? item.name : item.agentInsightId} count={item.affectedSessionCount} max={max}
                extra={item.lastSeenAt ? <small>{t("agentFindings.lastSeen", { time: new Date(item.lastSeenAt).toLocaleString(i18n.language) })}</small> : undefined} />
            </button>
            {open && <LiveDetail key={item.agentInsightId} agent={agent} id={item.agentInsightId} skin={skin} base={base} />}
          </article>
        );
      })}
      {data.next_cursor && <div className="fnd-pager"><skin.Button onClick={() => next(data.next_cursor!)}>{t("agentFindings.next")}</skin.Button></div>}
    </div>
  );
}

function LiveAgent({ agent, skin, base, emptyKey = "agentFindings.noLive" }: {
  agent: string; skin: FindingsSkin; base: string; emptyKey?: string;
}) {
  const { t } = useTranslation();
  const [pages, setPages] = useState<string[]>([]);
  const cursor = pages.at(-1) ?? null;
  return (
    <>
      <LivePage key={cursor ?? ""} agent={agent} cursor={cursor} next={(s) => setPages([...pages, s])} skin={skin} base={base} emptyKey={emptyKey} />
      {pages.length > 0 && <div className="fnd-pager"><skin.Button onClick={() => setPages(pages.slice(0, -1))}>{t("agentFindings.previous")}</skin.Button></div>}
    </>
  );
}

const STORE_TONE: Record<string, NoteTone> = { updating: "info", disabled: "warn", error: "error", denied: "warn", unsupported: "warn", unavailable: "warn" };

/** Renders `children` only once the regional Insight Store is ready; otherwise says why not. */
function StoreGate({ skin, children }: { skin: FindingsSkin; children: ReactNode }) {
  const { t } = useTranslation();
  const store = useLoad(() => api.insightStore(), "finding-store");
  if (store.loading) return <skin.Busy />;
  if (store.error) return <LoadProblem skin={skin} message={store.error} retry={store.reload} />;
  if (store.data?.state !== "ready") {
    return store.data && (
      <skin.Note tone={STORE_TONE[store.data.state] ?? "warn"} action={<skin.Button onClick={store.reload}>{t("agentFindings.refresh")}</skin.Button>}>
        {t(`agentFindings.store.${store.data.state}`)}
      </skin.Note>
    );
  }
  return <>{children}</>;
}

function LiveBrowser({ skin, base }: { skin: FindingsSkin; base: string }) {
  return <StoreGate skin={skin}><AgentPicker skin={skin} base={base} /></StoreGate>;
}

function AgentPicker({ skin, base }: { skin: FindingsSkin; base: string }) {
  const { t } = useTranslation();
  const agents = useLoad(() => api.listAgents(), "finding-agents");
  const [agent, setAgent] = useState("");
  const options = (agents.data?.agents ?? [])
    .filter((a) => a.resource_id && a.status === "active")
    .map((a) => ({ value: a.id, label: a.display_name || a.name }));
  return (
    <div className="fnd-stack" data-testid="online-findings">
      <p className="fnd-hint">{t("agentFindings.liveHint")}</p>
      {agents.error
        ? <LoadProblem skin={skin} message={agents.error} retry={agents.reload} />
        : <div className="fnd-pick"><skin.Pick value={agent} options={options} placeholder={t("agentFindings.pickAgent")} onChange={setAgent} ariaLabel={t("agentFindings.agent")} testId="findings-agent" /></div>}
      {agent && <LiveAgent key={agent} agent={agent} skin={skin} base={base} />}
    </div>
  );
}

/**
 * The agent's ongoing patterns, on an online config that runs Failure Analysis. The
 * config's session scores (below it) and these patterns are separate reads: AgentCore
 * groups related failures for the whole endpoint, so a pattern is not owned by one config
 * and can lag behind the scores.
 */
export function ConfigPatterns({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const { skin, base } = useConsole();
  return (
    <skin.Section title={t("agentFindings.configTitle")} testId="config-patterns">
      <div className="fnd-stack">
        <p className="fnd-hint">{t("agentFindings.configHint")}</p>
        <StoreGate skin={skin}><LiveAgent agent={agentId} skin={skin} base={base} emptyKey="agentFindings.noLiveConfig" /></StoreGate>
      </div>
    </skin.Section>
  );
}

/** Read only the active source. Online counts refer to the agent endpoint's history,
 * while the overview's time range selects batch runs. Page shells remount per project. */
export function FindingsBrowser({ runs, history, busy = false }: {
  runs: { id: string; name: string; status: string }[]; history: ReactNode; busy?: boolean;
}) {
  const { t } = useTranslation();
  const { skin, base } = useConsole();
  const [source, setSource] = useState<"batch" | "online" | "history">("batch");
  const [run, setRun] = useState("");
  const selected = runs.find((r) => r.id === run);
  return (
    <section className="fnd-browser" data-testid="findings-browser">
      <div className="fnd-switch">
        <h2>{t("agentFindings.title")}</h2>
        <skin.Tabs value={source} onChange={(v) => setSource(v as typeof source)} ariaLabel={t("agentFindings.title")}
          options={(["batch", "online", "history"] as const).map((key) => ({ value: key, label: t(`agentFindings.${key}`) }))} />
      </div>
      {source === "history" ? history : source === "online" ? (
        <skin.Section title={t("agentFindings.online")}><LiveBrowser skin={skin} base={base} /></skin.Section>
      ) : (
        <skin.Section title={t("agentFindings.batch")}>
          <div className="fnd-stack">
            <p className="fnd-hint">{t("agentFindings.batchHint")}</p>
            {runs.length > 0
              ? <div className="fnd-pick"><skin.Pick value={selected?.id ?? ""} options={runs.map((r) => ({ value: r.id, label: r.name }))}
                  placeholder={t("agentFindings.pickRun")} onChange={setRun} ariaLabel={t("agentFindings.run")} testId="findings-run" /></div>
              : <skin.Note tone="info">{t(busy ? "agentFindings.loading" : "agentFindings.noBatch")}</skin.Note>}
            {selected && <BatchBody key={selected.id} runId={selected.id} status={selected.status} skin={skin} base={base} />}
          </div>
        </skin.Section>
      )}
    </section>
  );
}
