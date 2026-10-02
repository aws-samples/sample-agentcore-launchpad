import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api, errorMessage, type RuntimeCanaryInfo } from "../../../lib/api";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Confirm, Select, Tag } from "../../ui";
import { CANARY_TONE, openingWeights, versionOptions, versionsLabel, weightsLabel } from "../canary/common";
import { RampPlan } from "../canary/RampPlan";

const POLL_MS = 8000;

const byNumber = (a: string, b: string) => Number(a) - Number(b);

/**
 * 金丝雀实验 (next steps, step 4) — A/B the Harness's latest version (the one
 * accepted in step 3) against an earlier version, default the first. Creating the
 * canary is behind a confirm and runs `setup` right away (dedicated gateway, two
 * Harness endpoints, per-variant online evals, an A/B test at 90/10 — or 50/50 when
 * the ramp plan drops 90/10); traffic, verdict,
 * ramp, promote/rollback and cleanup live on the canary's detail page.
 */
export function HarnessCanary({
  agentId, agentName, agentActive, versionsKey, account, region,
}: {
  agentId: string;
  agentName: string;
  agentActive: boolean;
  /** changes when a new version was published, so the version list re-reads */
  versionsKey: string;
  account: string;
  region: string;
}) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useV2Toast();
  const [tick, setTick] = useState(0);
  const versionsLoad = useLoad(() => api.agentVersions(agentId), `versions:${agentId}:${versionsKey}`);
  const canariesLoad = useLoad(() => api.listRuntimeCanaries(), `canaries:${agentId}:${tick}`);
  const versions = useMemo(
    () => [...new Set((versionsLoad.data?.versions ?? []).map((v) => v.version).filter((v): v is string => !!v))].sort(byNumber),
    [versionsLoad.data],
  );
  const latest = versions[versions.length - 1] ?? null;
  const earlier = versions.filter((v) => v !== latest);
  const [control, setControl] = useState("");
  // default the FIRST version; keep a still-valid pick across re-reads
  useEffect(() => {
    setControl((prev) => (earlier.includes(prev) ? prev : (earlier[0] ?? "")));
  }, [earlier.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps

  const canaries = (canariesLoad.data?.canaries ?? []).filter((c) => c.champion_agent_id === agentId);
  const current: RuntimeCanaryInfo | null = canaries.find((c) => c.status === "running") ?? canaries[0] ?? null;
  const live = current?.status === "running";
  const busyAction = !!current?.running_action;
  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => setTick((n) => n + 1), POLL_MS);
    return () => window.clearInterval(timer);
  }, [live]);

  const [confirm, setConfirm] = useState(false);
  const [runFirst, setRunFirst] = useState(true);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mayCreate = can("eval.run");
  const blocked = !mayCreate
    ? t("assistantNext.canary.noPermission")
    : !agentActive
      ? t("assistantNext.canary.agentNotActive")
      : versions.length < 2
        ? t("assistantNext.canary.needsTwoVersions")
        : live
          ? t("assistantNext.canary.alreadyRunning")
          : undefined;

  const create = async () => {
    if (!latest || !control) return;
    setCreating(true);
    setError(null);
    try {
      const row = await api.createRuntimeCanary({
        agent_id: agentId,
        harness_versions: { control, treatment: latest },
        start_stage: runFirst ? 0 : 1,
      });
      await api.runtimeCanaryAction(row.id, { action: "setup" });
      toast("success", t("assistantNext.canary.startedToast", { name: row.name }));
      setTick((n) => n + 1);
    } catch (err) {
      const message = errorMessage(err);
      setError(message);
      toast("error", message);
    } finally {
      setCreating(false);
    }
  };

  const detailLink = (id: string) => `/v2/eval/experiments?mode=canary&canary=${encodeURIComponent(id)}`;
  const setup = current?.artifacts.setup;

  return (
    <div className="v2-assistant-sub-rows" data-testid="v2-assistant-next-canary" data-canary-status={current?.status ?? "none"}>
      <div style={{ fontWeight: 600 }}>{t("assistantNext.canary.title")}</div>
      <div className="v2-muted">{t("assistantNext.canary.body")}</div>
      <div className="v2-assistant-sub-row">
        <span>{t("assistantNext.canary.versions")}</span>
        {versionsLoad.loading && !versionsLoad.data ? (
          <span className="v2-muted">…</span>
        ) : versions.length > 0 ? (
          <span className="v2-tags">
            {versions.map((v) => (
              <Tag key={v} tone={v === latest ? "green" : "outline"}>
                {v === latest ? t("assistantNext.canary.latest", { v }) : `v${v}`}
              </Tag>
            ))}
          </span>
        ) : (
          <span className="v2-muted">—</span>
        )}
      </div>
      {!live && (
        <div className="v2-assistant-sub-row">
          <label htmlFor="v2-canary-control">{t("assistantNext.canary.control")}</label>
          <Select
            id="v2-canary-control"
            style={{ width: "auto", minWidth: 140 }}
            value={control}
            options={versionOptions(earlier, t("assistantNext.canary.firstSuffix"))}
            disabled={earlier.length === 0 || creating}
            onChange={setControl}
            testId="v2-assistant-next-canary-control"
          />
          <span>{t("assistantNext.canary.treatment", { v: latest ?? "—" })}</span>
          <Button
            size="sm"
            kind="primary"
            disabled={!!blocked || creating || !control || !latest}
            title={blocked}
            onClick={() => setConfirm(true)}
            testId="v2-assistant-next-canary-create"
          >
            {creating ? t("assistantNext.canary.creating") : t("assistantNext.canary.create")}
          </Button>
          {blocked && <span className="v2-muted" style={{ fontSize: 12.5 }}>{blocked}</span>}
        </div>
      )}
      {!live && <RampPlan runFirst={runFirst} onChange={setRunFirst} disabled={creating} />}
      {current && (
        <div className="v2-assistant-sub-row" data-testid="v2-assistant-next-canary-current">
          <span>{t("assistantNext.canary.current")}</span>
          <Tag tone={CANARY_TONE[current.status] ?? "gray"} dot>{t(`v2.canary.status.${current.status}`)}</Tag>
          <span className="mono">{current.name}</span>
          <span className="v2-muted mono">{versionsLabel(setup)}</span>
          {setup?.ab_test_id && <span className="v2-muted mono">{weightsLabel(setup)}</span>}
          {busyAction && <span className="v2-muted">{current.progress ?? t("canaryPage.running")}</span>}
          <Link to={detailLink(current.id)} data-testid="v2-assistant-next-canary-open">{t("assistantNext.canary.open")} ›</Link>
          {current.error && <div className="err">{current.error}</div>}
        </div>
      )}
      {(error || versionsLoad.error || canariesLoad.error) && (
        <Alert tone="error">{error ?? versionsLoad.error ?? canariesLoad.error}</Alert>
      )}
      <Confirm
        open={confirm}
        title={t("assistantNext.canary.confirmTitle")}
        body={t("assistantNext.canary.confirmBody", {
          name: agentName, control, treatment: latest ?? "—", account, region, weights: openingWeights(runFirst),
        })}
        confirmLabel={t("assistantNext.canary.create")}
        onConfirm={() => { setConfirm(false); void create(); }}
        onClose={() => setConfirm(false)}
      />
    </div>
  );
}
