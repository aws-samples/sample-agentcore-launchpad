import { RefreshCw } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { methodLabel } from "../../components/methodChipMeta";
import { api, type DriftAgent, type DriftReport, type EnvironmentRow, errorMessage } from "../../lib/api";
import { useWorkspace } from "../../workspace/workspace-context";
import { fmtDuration, fmtTime } from "../format";
import { useLoad } from "../hooks";
import { SIGNAL_TONE } from "../signals";
import { Alert, Button, Card, type Column, Kpi, Lamp, PageHeader, Select, SubTabs, Table, Tag } from "../ui";
import {
  changedGroupKeys,
  COMPARE_SIGNAL,
  compareStateKey,
  compareSummary,
  DRIFT_SIGNAL,
  driftDetail,
  driftStateKey,
  rankDrift,
} from "./environments/states";

type EnvView = "compare" | "drift";

// Switching workspace clears the URL's search (WorkspaceProvider), but compare is keyed
// on a name, not an id: the last compared name is kept for the tab so a switch only
// moves the baseline.
const LAST_AGENT_KEY = "launchpad.v2.environments.agent";

function rememberAgent(name: string) {
  try {
    if (name) sessionStorage.setItem(LAST_AGENT_KEY, name);
    else sessionStorage.removeItem(LAST_AGENT_KEY);
  } catch {
    // storage unavailable: a switch just resets the picker
  }
}

function lastAgent(): string {
  try {
    return sessionStorage.getItem(LAST_AGENT_KEY) ?? "";
  } catch {
    return "";
  }
}

/* ---------- compare ---------- */

function CompareView({ agent, onAgent }: { agent: string; onAgent: (name: string) => void }) {
  const { t } = useTranslation();
  const { current } = useWorkspace();
  // the agents of the current workspace the caller can see (the server filters)
  const agents = useLoad(() => api.listAgents(), "v2-env-agents");
  const [nonce, setNonce] = useState(0);
  const result = useLoad(
    () => (agent ? api.environmentCompare(agent) : Promise.resolve(null)),
    `v2-env-compare:${agent}:${nonce}`,
  );

  const names = useMemo(() => {
    const live = (agents.data?.agents ?? []).filter((a) => a.status !== "deleted").map((a) => a.name);
    // a name from the URL stays pickable even when this workspace has no such agent
    return [...new Set([...live, ...(agent ? [agent] : [])])].sort((a, b) => a.localeCompare(b));
  }, [agents.data, agent]);

  const columns: Column<EnvironmentRow>[] = [
    {
      key: "workspace",
      title: t("v2.environments.colWorkspace"),
      render: (row) => (
        <>
          <span>{row.workspace.name}</span>
          <span className="sub mono">
            {row.workspace.account_id} · {row.workspace.region}
          </span>
        </>
      ),
    },
    {
      key: "state",
      title: t("v2.environments.colState"),
      className: "nowrap",
      render: (row) => {
        const signal = COMPARE_SIGNAL[row.state];
        return (
          <span className="v2-row" style={{ gap: 6, flexWrap: "nowrap" }} data-testid={`v2-env-state-${row.workspace.id}`}>
            <Lamp signal={signal} />
            <Tag tone={SIGNAL_TONE[signal]}>{t(compareStateKey(row.state))}</Tag>
          </span>
        );
      },
    },
    {
      key: "differs",
      title: t("v2.environments.colDiffers"),
      render: (row) =>
        row.changed_groups.length ? (
          <span className="v2-tags">
            {changedGroupKeys(row.changed_groups).map((key, i) => (
              <Tag key={`${key}:${i}`} tone="outline">
                {t(key)}
              </Tag>
            ))}
          </span>
        ) : (
          <span className="v2-muted">—</span>
        ),
    },
    {
      key: "status",
      title: t("v2.environments.colStatus"),
      className: "nowrap",
      render: (row) =>
        row.agent ? t(`status.${row.agent.status}`, { defaultValue: row.agent.status }) : <span className="v2-muted">—</span>,
    },
    {
      key: "version",
      title: t("v2.environments.colVersion"),
      className: "nowrap",
      render: (row) => <span className="mono">{row.agent?.version ? `v${row.agent.version}` : "—"}</span>,
    },
    {
      key: "deploy",
      title: t("v2.environments.colDeploy"),
      className: "nowrap",
      render: (row) =>
        row.agent?.last_deploy ? (
          <>
            <span>{fmtTime(row.agent.last_deploy.started_at)}</span>
            <span className="sub">
              {t(`v2.agents.stage.${row.agent.last_deploy.status}`, { defaultValue: row.agent.last_deploy.status })}
            </span>
          </>
        ) : row.agent ? (
          <span className="v2-muted">{t("v2.environments.notDeployed")}</span>
        ) : (
          <span className="v2-muted">—</span>
        ),
    },
    {
      key: "fingerprint",
      title: t("v2.environments.colFingerprint"),
      render: (row) =>
        row.agent ? (
          <>
            <span className="mono" title={row.agent.spec_digest}>
              {row.agent.spec_digest.slice(0, 12)}
            </span>
            {row.agent.image_digest && (
              <span className="sub mono" title={row.agent.image_digest}>
                {t("v2.environments.image", { digest: row.agent.image_digest.replace(/^sha256:/, "").slice(0, 12) })}
              </span>
            )}
          </>
        ) : (
          <span className="v2-muted">—</span>
        ),
    },
  ];

  const data = result.data;
  const summary = data ? compareSummary(data) : null;

  return (
    <>
      <div className="v2-toolbar">
        <Select
          value={agent}
          options={names.map((name) => ({ value: name, label: name }))}
          onChange={onAgent}
          placeholder={t("v2.environments.agentPlaceholder")}
          disabled={agents.loading && !agents.data}
          ariaLabel={t("v2.environments.agentLabel")}
          style={{ minWidth: 260 }}
          testId="v2-env-agent"
        />
        <span className="v2-muted" data-testid="v2-env-baseline">
          {t("v2.environments.baselineNote", { workspace: current?.name ?? "—" })}
        </span>
      </div>
      {agents.error && <Alert tone="error">{agents.error}</Alert>}
      {!agent ? (
        <Card testId="v2-env-pick">
          <p className="v2-muted">
            {agents.data && names.length === 0 ? t("v2.environments.noAgents") : t("v2.environments.pickAgent")}
          </p>
        </Card>
      ) : (
        <Card
          title={t("v2.environments.compareTitle", { agent })}
          sub={t("v2.environments.compareSub")}
          flush
          end={
            <Button size="sm" disabled={result.loading} onClick={() => setNonce((n) => n + 1)} testId="v2-env-compare-refresh">
              <RefreshCw size={13} aria-hidden="true" />
              {t("v2.common.refresh")}
            </Button>
          }
          testId="v2-env-compare"
        >
          <div style={{ padding: "0 24px 16px" }} className="v2-stack">
            {summary && (
              <div data-testid="v2-env-summary">
                <Alert tone={summary.tone}>{t(summary.key, summary.params)}</Alert>
              </div>
            )}
            <Table
              columns={columns}
              rows={data?.environments ?? []}
              rowKey={(row) => row.workspace.id}
              loading={result.loading}
              error={result.error}
              onRetry={result.reload}
              testId="v2-env-compare-table"
            />
            <Alert tone="info">{t("v2.environments.matchNote")}</Alert>
          </div>
        </Card>
      )}
    </>
  );
}

/* ---------- drift ---------- */

function versionCell(row: DriftAgent): string {
  const ledger = row.ledger_version ?? "—";
  const aws = row.aws_version ?? "—";
  return ledger === aws ? ledger : `${ledger} → ${aws}`;
}

function DriftView() {
  const { t } = useTranslation();
  const [report, setReport] = useState<DriftReport | null>(null);
  const [checkedAt, setCheckedAt] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // a second click lands before React re-renders the disabled button
  const inFlight = useRef(false);

  const check = async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      setReport(await api.environmentDrift());
      setCheckedAt(new Date().toISOString());
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };

  const columns: Column<DriftAgent>[] = [
    {
      key: "agent",
      title: t("v2.environments.colAgent"),
      render: (row) => (
        <>
          <Link className="v2-link" to={`/v2/agents?view=detail&id=${encodeURIComponent(row.agent_id)}`}>
            {row.name}
          </Link>
          <span className="sub">{methodLabel(row.method)}</span>
        </>
      ),
    },
    {
      key: "state",
      title: t("v2.environments.colState"),
      className: "nowrap",
      render: (row) => {
        const signal = DRIFT_SIGNAL[row.state];
        return (
          <span className="v2-row" style={{ gap: 6, flexWrap: "nowrap" }} data-testid={`v2-drift-state-${row.agent_id}`}>
            <Lamp signal={signal} />
            <Tag tone={SIGNAL_TONE[signal]}>{t(driftStateKey(row.state))}</Tag>
          </span>
        );
      },
    },
    {
      key: "versions",
      title: t("v2.environments.colVersions"),
      className: "nowrap",
      render: (row) => <span className="mono">{versionCell(row)}</span>,
    },
    {
      key: "aws",
      title: t("v2.environments.colAwsStatus"),
      className: "nowrap",
      render: (row) => <span className="mono">{row.aws_status ?? "—"}</span>,
    },
    {
      key: "detail",
      title: t("v2.environments.colDetail"),
      render: (row) => {
        const phrases = driftDetail(row);
        return phrases.length ? phrases.map((p) => t(p.key, p.params)).join("; ") : <span className="v2-muted">—</span>;
      },
    },
  ];

  const overall = report ? DRIFT_SIGNAL[report.state] : null;

  return (
    <Card
      title={t("v2.environments.driftTitle")}
      sub={t("v2.environments.driftSub")}
      end={
        <span className="v2-row">
          {report && overall && (
            <span className="v2-row" style={{ gap: 6 }} data-testid="v2-drift-overall">
              <Lamp signal={overall} />
              <Tag tone={SIGNAL_TONE[overall]}>{t(driftStateKey(report.state))}</Tag>
            </span>
          )}
          <Button kind="primary" size="sm" disabled={busy} onClick={() => void check()} testId="v2-drift-check">
            {busy ? t("v2.environments.checking") : t("v2.environments.check")}
          </Button>
        </span>
      }
      testId="v2-env-drift"
    >
      <div className="v2-stack">
        <Alert tone="info">{t("v2.environments.driftNote")}</Alert>
        {error && (
          <div data-testid="v2-drift-error">
            <Alert tone="error">{error}</Alert>
          </div>
        )}
        {!report ? (
          <p className="v2-muted" data-testid="v2-drift-idle">
            {t("v2.environments.notChecked")}
          </p>
        ) : (
          <>
            <div className="v2-kpis">
              <Kpi
                label={t("v2.environments.kpiChecked")}
                value={report.checked}
                sub={t("v2.environments.checkedAt", {
                  time: fmtTime(checkedAt),
                  elapsed: fmtDuration(report.elapsed_ms),
                })}
                testId="v2-drift-kpi-checked"
              />
              <Kpi
                label={t("v2.environments.kpiInSync")}
                value={report.counts.in_sync}
                tone={report.checked > 0 && report.counts.in_sync === report.checked ? "good" : undefined}
                testId="v2-drift-kpi-in-sync"
              />
              <Kpi
                label={t("v2.environments.kpiDrift")}
                value={report.counts.drift}
                tone={report.counts.drift > 0 ? "bad" : undefined}
                testId="v2-drift-kpi-drift"
              />
              <Kpi
                label={t("v2.environments.kpiUnknown")}
                value={report.counts.unknown}
                sub={t("v2.environments.unknownSub")}
                testId="v2-drift-kpi-unknown"
              />
            </div>
            {report.truncated && (
              <Alert tone="warn">
                <span data-testid="v2-drift-truncated">{t("v2.environments.truncated", { limit: report.limit })}</span>
              </Alert>
            )}
            {report.counts.unknown > 0 && <Alert tone="info">{t("v2.environments.unknownNote")}</Alert>}
            <Table
              columns={columns}
              rows={rankDrift(report.agents)}
              rowKey={(row) => row.agent_id}
              empty={t("v2.environments.noLiveAgents")}
              testId="v2-drift-table"
            />
          </>
        )}
      </div>
    </Card>
  );
}

/**
 * 环境对比 — one agent lined up across every workspace the caller can see, against the
 * workspace selected in the header (ledger only), and a drift check of that workspace's
 * agents against what AWS reports (versions and status; on request only). Members see
 * their granted workspaces and, in each one, only the agents its visibility mode shows
 * them — the server filters both. `?view=compare` (default, `agent=`) | `?view=drift`.
 */
export function V2Environments() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const view: EnvView = params.get("view") === "drift" ? "drift" : "compare";
  const agent = params.get("agent") ?? "";

  const go = (patch: { view?: EnvView; agent?: string }, replace = false) => {
    const next = new URLSearchParams(params);
    next.set("view", patch.view ?? view);
    const name = patch.agent ?? agent;
    if (patch.agent !== undefined) rememberAgent(name);
    if (name) next.set("agent", name);
    else next.delete("agent");
    setParams(next, { replace });
  };

  // a link (`?agent=`) wins; otherwise the name compared last, e.g. before a switch
  useEffect(() => {
    if (agent) {
      rememberAgent(agent);
      return;
    }
    if (view !== "compare") return;
    const last = lastAgent();
    if (!last) return;
    const next = new URLSearchParams(params);
    next.set("view", "compare");
    next.set("agent", last);
    setParams(next, { replace: true });
  }, [agent, view, params, setParams]);

  return (
    <>
      <PageHeader
        title={t("v2.environments.title")}
        desc={t("v2.environments.desc")}
        tabs={
          <SubTabs
            value={view}
            onChange={(next) => go({ view: next })}
            tabs={(["compare", "drift"] as const).map((value) => ({ value, label: t(`v2.environments.tab.${value}`) }))}
          />
        }
      />
      {view === "compare" ? (
        <CompareView agent={agent} onAgent={(name) => go({ view: "compare", agent: name }, true)} />
      ) : (
        <DriftView />
      )}
    </>
  );
}
