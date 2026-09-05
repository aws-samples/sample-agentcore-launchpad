import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { RefreshCw } from "lucide-react";

import type { AgentVersionsInfo } from "../lib/api";
import { api, ApiError } from "../lib/api";
import { Btn } from "./Btn";
import { Chip, type ChipTone } from "./Chip";
import { LoadError } from "./LoadError";
import { Panel } from "./Panel";

/**
 * VERSIONS & ENDPOINTS — the read-only AWS view of one agent's Runtime or
 * Harness: every immutable version and every endpoint (DEFAULT + named).
 *
 * Strictly read-only: no re-pointing of DEFAULT, no endpoint create/update/
 * delete — the target canary owns those. What it makes visible:
 *  - the DEFAULT endpoint (auto-follows the latest version);
 *  - the version the ledger recorded at deploy time vs the AWS latest — a
 *    mismatch (out-of-band update, canary candidate mint) is shown, not an error;
 *  - `stable`/`treatment` named endpoints, so canary leftovers are noticed.
 */

type State =
  | { phase: "loading" }
  | { phase: "ready"; data: AgentVersionsInfo }
  | { phase: "no_resource"; reason: string }
  | { phase: "error"; message: string };

function statusTone(status: string | null): ChipTone {
  const s = (status ?? "").toUpperCase();
  if (s === "READY") return "good";
  if (s.endsWith("_FAILED")) return "crit";
  if (s === "CREATING" || s === "UPDATING" || s === "DELETING") return "warn";
  return "muted";
}

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function VersionsPanel({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const [state, setState] = useState<State>({ phase: "loading" });

  const load = useCallback(async () => {
    setState({ phase: "loading" });
    try {
      const data = await api.agentVersions(agentId);
      setState({ phase: "ready", data });
    } catch (e) {
      if (e instanceof ApiError && e.code === "agent.no_resource") {
        setState({ phase: "no_resource", reason: e.message });
      } else {
        setState({ phase: "error", message: e instanceof Error ? e.message : String(e) });
      }
    }
  }, [agentId]);

  useEffect(() => {
    void load();
  }, [load]);

  const data = state.phase === "ready" ? state.data : null;
  const mismatch =
    data != null &&
    data.ledger_version != null &&
    data.latest_version != null &&
    data.ledger_version !== data.latest_version;

  return (
    <div data-testid="versions-panel" data-phase={state.phase}>
      <Panel
        title={t("create.versions.title")}
        sub={
          data
            ? `${t(data.kind === "harness" ? "create.versions.kindHarness" : "create.versions.kindRuntime")} · ${data.resource_id}`
            : t("create.versions.sub")
        }
        end={
          <Btn
            onClick={() => void load()}
            disabled={state.phase === "loading"}
            title={t("create.versions.refresh")}
            data-testid="versions-refresh"
          >
            <RefreshCw size={12} />
          </Btn>
        }
      >
        {state.phase === "loading" && (
          <div className="dim mono" data-testid="versions-loading">
            {t("common.loading")}
          </div>
        )}
        {state.phase === "error" && (
          <LoadError
            inline
            message={state.message}
            onRetry={() => void load()}
            data-testid="versions-error"
          />
        )}
        {state.phase === "no_resource" && (
          <div className="note" data-testid="versions-no-resource">
            <span className="i">[i]</span>
            <span>{state.reason}</span>
          </div>
        )}
        {data && (
          <>
            <div className="kv" data-testid="versions-summary">
              <span className="k">{t("create.versions.ledgerVsLatest")}</span>
              <span className="v" style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
                <span>
                  {t("create.versions.ledger")} v{data.ledger_version ?? "—"} ·{" "}
                  {t("create.versions.latest")} v{data.latest_version ?? "—"}
                </span>
                {mismatch ? (
                  <span data-testid="versions-mismatch">
                    <Chip tone="warn">{t("create.versions.mismatch")}</Chip>
                  </span>
                ) : data.ledger_version != null && data.latest_version != null ? (
                  <span data-testid="versions-in-sync">
                    <Chip tone="good">{t("create.versions.inSync")}</Chip>
                  </span>
                ) : null}
              </span>
            </div>
            {mismatch && (
              <div className="dim" style={{ fontSize: 11, marginBottom: 8 }}>
                {t("create.versions.mismatchHint", {
                  ledger: data.ledger_version,
                  latest: data.latest_version,
                })}
              </div>
            )}
            {data.canary_endpoints.length > 0 && (
              <div className="note" style={{ marginBottom: 10 }} data-testid="versions-canary-note">
                <span className="i">[!]</span>
                <span>
                  {t("create.versions.canaryLeftovers", {
                    names: data.canary_endpoints.join(", "),
                  })}
                </span>
              </div>
            )}

            <div className="mono dim" style={{ fontSize: 10, letterSpacing: ".12em", margin: "10px 0 4px" }}>
              {t("create.versions.versionsHead", { n: data.versions.length })}
            </div>
            {data.versions.length === 0 ? (
              <div className="dim" style={{ fontSize: 11.5 }} data-testid="versions-empty">
                {t("create.versions.noVersions")}
              </div>
            ) : (
              <table data-testid="versions-table">
                <thead>
                  <tr>
                    <th>{t("create.versions.colVersion")}</th>
                    <th>{t("create.versions.colStatus")}</th>
                    <th>{t("create.versions.colDescription")}</th>
                    <th>{t("create.versions.colUpdated")}</th>
                  </tr>
                </thead>
                <tbody>
                  {data.versions.map((v) => {
                    const isLedger = v.version != null && v.version === data.ledger_version;
                    const isLatest = v.version != null && v.version === data.latest_version;
                    return (
                      <tr
                        key={v.version ?? "?"}
                        data-testid="version-row"
                        data-version={v.version ?? ""}
                        data-ledger={isLedger ? "true" : undefined}
                        style={isLedger ? { background: "rgba(255,176,0,.045)" } : undefined}
                      >
                        <td className="pri mono" style={{ whiteSpace: "nowrap" }}>
                          v{v.version ?? "?"}
                          {isLatest && (
                            <>
                              {" "}
                              <Chip tone="blue">{t("create.versions.tagLatest")}</Chip>
                            </>
                          )}
                          {isLedger && (
                            <>
                              {" "}
                              <Chip tone="amber">{t("create.versions.tagLedger")}</Chip>
                            </>
                          )}
                        </td>
                        <td>
                          <Chip tone={statusTone(v.status)}>{v.status ?? "—"}</Chip>
                        </td>
                        <td className="dim" style={{ fontSize: 11.5 }}>
                          {v.description ?? "—"}
                        </td>
                        <td className="mono dim">{fmtTime(v.last_updated_at)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}

            <div className="mono dim" style={{ fontSize: 10, letterSpacing: ".12em", margin: "14px 0 4px" }}>
              {t("create.versions.endpointsHead", { n: data.endpoints.length })}
            </div>
            {data.endpoints.length === 0 ? (
              <div className="dim" style={{ fontSize: 11.5 }} data-testid="endpoints-empty">
                {t("create.versions.noEndpoints")}
              </div>
            ) : (
              <table data-testid="endpoints-table">
                <thead>
                  <tr>
                    <th>{t("create.versions.colEndpoint")}</th>
                    <th>{t("create.versions.colLive")}</th>
                    <th>{t("create.versions.colStatus")}</th>
                    <th>{t("create.versions.colUpdated")}</th>
                  </tr>
                </thead>
                <tbody>
                  {data.endpoints.map((e) => {
                    const isDefault = e.name === "DEFAULT";
                    const isCanary = e.name != null && data.canary_endpoints.includes(e.name);
                    const rolling =
                      e.target_version != null &&
                      e.live_version != null &&
                      e.target_version !== e.live_version;
                    return (
                      <tr
                        key={e.name ?? "?"}
                        data-testid="endpoint-row"
                        data-endpoint={e.name ?? ""}
                        data-default={isDefault ? "true" : undefined}
                        data-canary={isCanary ? "true" : undefined}
                      >
                        <td className="pri mono" style={{ whiteSpace: "nowrap" }}>
                          {e.name ?? "—"}
                          {isDefault && (
                            <>
                              {" "}
                              <Chip tone="blue">{t("create.versions.tagDefault")}</Chip>
                            </>
                          )}
                          {isCanary && (
                            <>
                              {" "}
                              <Chip tone="amber">{t("create.versions.tagCanary")}</Chip>
                            </>
                          )}
                          {e.description && (
                            <div className="dim" style={{ fontSize: 10.5, fontFamily: "inherit" }}>
                              {e.description}
                            </div>
                          )}
                        </td>
                        <td className="mono">
                          v{e.live_version ?? "?"}
                          {rolling && (
                            <span className="dim"> → v{e.target_version}</span>
                          )}
                        </td>
                        <td>
                          <Chip tone={statusTone(e.status)}>{e.status ?? "—"}</Chip>
                          {e.failure_reason && (
                            <div className="dim" style={{ fontSize: 10.5, marginTop: 4 }}>
                              {e.failure_reason}
                            </div>
                          )}
                        </td>
                        <td className="mono dim">{fmtTime(e.last_updated_at)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
            <div className="dim" style={{ fontSize: 10.5, marginTop: 10 }}>
              {t("create.versions.readOnlyHint")}
            </div>
          </>
        )}
      </Panel>
    </div>
  );
}
