import { RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, type EnvironmentRow } from "../../lib/api";
import { useWorkspace } from "../../workspace/workspace-context";
import { fmtTime } from "../format";
import { useLoad } from "../hooks";
import { SIGNAL_TONE } from "../signals";
import { Alert, Button, Card, type Column, Lamp, PageHeader, Select, Table, Tag } from "../ui";
import { changedGroupKeys, COMPARE_SIGNAL, compareStateKey, compareSummary } from "./environments/states";

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

/**
 * 环境对比 — one agent lined up across every workspace the caller can see, against the
 * workspace selected in the header (ledger only). Members see their granted workspaces
 * and, in each one, only the agents its visibility mode shows them — the server filters
 * both. `?agent=` picks the agent. The drift check (`GET /api/environments/drift`) is
 * API-only: the console does not show it (2026-10-14, user decision — a version-only
 * signal that on shared-AWS boxes is mostly the other console's redeploys).
 */
export function V2Environments() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const agent = params.get("agent") ?? "";

  const pick = (name: string) => {
    const next = new URLSearchParams(params);
    next.delete("view"); // a bookmarked `?view=drift` lands here too
    rememberAgent(name);
    if (name) next.set("agent", name);
    else next.delete("agent");
    setParams(next, { replace: true });
  };

  // a link (`?agent=`) wins; otherwise the name compared last, e.g. before a switch
  useEffect(() => {
    if (agent) {
      rememberAgent(agent);
      return;
    }
    const last = lastAgent();
    if (!last) return;
    const next = new URLSearchParams(params);
    next.set("agent", last);
    setParams(next, { replace: true });
  }, [agent, params, setParams]);

  return (
    <>
      <PageHeader title={t("v2.environments.title")} desc={t("v2.environments.desc")} />
      <CompareView agent={agent} onAgent={pick} />
    </>
  );
}
