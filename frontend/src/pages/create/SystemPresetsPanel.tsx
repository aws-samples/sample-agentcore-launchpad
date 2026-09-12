import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { Btn, Chip, ConfirmDialog, Panel, useToast } from "../../components";
import type { ChipTone } from "../../components";
import type { SystemPresetInfo, SystemPresetStatus } from "../../lib/api";
import { api, ApiError } from "../../lib/api";
import { useWorkspace } from "../../workspace/workspace-context";

const STATUS_TONE: Record<SystemPresetStatus, ChipTone> = {
  configuration_required: "muted",
  not_installed: "muted",
  deploying: "warn",
  uninstalling: "warn",
  active: "good",
  failed: "crit",
};

const TERMINAL: SystemPresetStatus[] = ["active", "failed", "not_installed"];
const IN_FLIGHT: SystemPresetStatus[] = ["deploying", "uninstalling"];
const POLL_MS = 4000;

type Operation = "install" | "repair" | "uninstall";

/**
 * System-managed presets: platform-owned agents an administrator installs on
 * purpose. Reads are ledger-only (`GET /api/system-agents` builds no AWS client),
 * so this panel may poll while a preset deploys; install/repair/uninstall are the
 * only paths that reach AWS and the server refuses them for non-administrators
 * regardless of what this component renders.
 *
 * Every asynchronous outcome is checked against the workspace it was started in
 * and against the mounted state, so an operation finishing after a workspace
 * switch (or after navigation) never announces into the wrong context.
 */
export function SystemPresetsPanel({
  onChanged,
  onDetails,
}: {
  /** fired after an install/repair/uninstall changed ledger state AND when a
   * deploying preset reaches a terminal status, so the parent list converges */
  onChanged?: () => void;
  /** open the deployment detail for the preset's agent */
  onDetails?: (agentId: string) => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const { isAdmin } = useAuth();
  const { current } = useWorkspace();
  const workspaceId = current?.id ?? null;
  const [presets, setPresets] = useState<SystemPresetInfo[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ kind: Operation; preset: SystemPresetInfo } | null>(
    null,
  );
  // stale-outcome guard: unmounted or a different workspace ⇒ ignore the result
  const alive = useRef(true);
  const scope = useRef(workspaceId);
  useEffect(() => {
    alive.current = true;
    scope.current = workspaceId;
    return () => {
      alive.current = false;
    };
  }, [workspaceId]);
  const stillCurrent = useCallback(
    (startedIn: string | null) => alive.current && scope.current === startedIn,
    [],
  );
  const previousStatuses = useRef<Record<string, SystemPresetStatus>>({});
  // refs keep `load` identity stable: a re-created `t` or parent callback must
  // not re-fire the mount effect and issue a duplicate status read
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;
  const tRef = useRef(t);
  tRef.current = t;

  const apiMessage = useCallback(
    (err: unknown) =>
      err instanceof ApiError ? tRef.current(`apiErrors.${err.code}`, err.message) : String(err),
    [],
  );

  const applyPresets = useCallback(
    (rows: SystemPresetInfo[]) => {
      setPresets(rows);
      setError(null);
      // a preset that just left `deploying` changes the agent list too
      const converged = rows.some((row) => {
        const before = previousStatuses.current[row.key];
        const settled = TERMINAL.includes(row.status);
        // a failed uninstall keeps `uninstalling` with a failed job — also terminal
        const uninstallSettled =
          row.status === "uninstalling" && row.operation?.job_status === "failed";
        return before !== undefined && IN_FLIGHT.includes(before) && (settled || uninstallSettled);
      });
      previousStatuses.current = Object.fromEntries(rows.map((row) => [row.key, row.status]));
      if (converged) onChangedRef.current?.();
    },
    [],
  );

  const load = useCallback(() => {
    const startedIn = scope.current;
    setLoading(true);
    void api
      .listSystemPresets()
      .then((res) => {
        if (!stillCurrent(startedIn)) return;
        applyPresets(res.presets);
      })
      .catch((err: unknown) => {
        if (!stillCurrent(startedIn)) return;
        setError(apiMessage(err));
      })
      .finally(() => {
        if (stillCurrent(startedIn)) setLoading(false);
      });
  }, [applyPresets, apiMessage, stillCurrent]);

  // Load once the workspace is resolved: a read fired before that would be
  // discarded as stale the moment the provider settles on an environment.
  useEffect(() => {
    if (workspaceId === null) return;
    load();
  }, [load, workspaceId]);
  // a deploying / uninstalling preset settles in ~30 s; poll the ledger read until
  // it does (a failed uninstall stops polling: its job is no longer live)
  const inFlight = (presets ?? []).some(
    (p) =>
      p.status === "deploying" ||
      (p.status === "uninstalling" && p.operation?.job_status !== "failed"),
  );
  useEffect(() => {
    if (!inFlight) return;
    const timer = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(timer);
  }, [inFlight, load]);

  const run = async (kind: Operation, preset: SystemPresetInfo) => {
    const startedIn = scope.current;
    setBusy(preset.key);
    try {
      if (kind === "uninstall") {
        const res = await api.uninstallSystemPreset(preset.key);
        if (!stillCurrent(startedIn)) return;
        // consume the outcome: the row is now `uninstalling` with a job, whatever the
        // follow-up GET does; the poll below carries it to not_installed or failure
        setPresets((prev) =>
          (prev ?? []).map((row) => (row.key === preset.key ? res.preset : row)),
        );
        toast(
          t(
            res.started
              ? res.attempt > 1
                ? "create.system.uninstallRetried"
                : "create.system.uninstallStarted"
              : "create.system.uninstallInFlight",
            { name: preset.label },
          ),
          "good",
        );
      } else {
        const res = await api.installSystemPreset(
          preset.key,
          kind === "repair" ? { force: true } : {},
        );
        if (!stillCurrent(startedIn)) return;
        // the response already carries the preset's new state — render it now so
        // a failed follow-up GET cannot leave a stale not-installed row behind
        setPresets((prev) =>
          (prev ?? []).map((row) => (row.key === preset.key ? res.preset : row)),
        );
        toast(
          t(
            res.changed
              ? res.created
                ? "create.system.installStarted"
                : "create.system.repairStarted"
              : "create.system.alreadyCurrent",
            { name: preset.label },
          ),
          "good",
        );
      }
      onChangedRef.current?.();
      load();
    } catch (err) {
      if (!stillCurrent(startedIn)) return;
      toast(apiMessage(err));
    } finally {
      if (stillCurrent(startedIn)) setBusy(null);
    }
  };

  const adminHint = isAdmin ? undefined : t("create.system.adminOnly");
  const requirementText = (preset: SystemPresetInfo) =>
    preset.requirements
      .map((req) => t(`create.system.requirementCodes.${req.code}`, req.message))
      .join("; ");

  return (
    <Panel
      title={t("create.system.title")}
      sub={t("create.system.sub")}
      className="system-presets"
      end={
        <Btn
          data-testid="system-presets-reload"
          disabled={loading}
          onClick={load}
          title={t("create.system.reload")}
        >
          {loading ? t("create.system.loading") : t("create.system.reload")}
        </Btn>
      }
    >
      {error && (
        <div className="note" style={{ borderColor: "var(--crit)" }} data-testid="system-presets-error">
          <span className="i" style={{ color: "var(--crit)" }}>[!]</span>
          <span>{t("create.system.loadFailed", { reason: error })}</span>
          <Btn data-testid="system-presets-retry" onClick={load} disabled={loading}>
            {t("create.system.retry")}
          </Btn>
        </div>
      )}
      {presets === null && !error && (
        <div className="dim mono" data-testid="system-presets-loading">
          {t("create.system.loading")}
        </div>
      )}
      {presets?.length === 0 && <div className="dim mono">{t("create.system.empty")}</div>}
      {(presets ?? []).map((preset) => {
        const installed = preset.agent_id != null;
        const blockers = preset.requirements.length > 0 ? requirementText(preset) : null;
        const collision = preset.name_collision
          ? t("create.system.collision", { name: preset.name_collision.agent_name })
          : null;
        const isBusy = busy === preset.key;
        return (
          <div
            key={preset.key}
            className="system-preset"
            data-testid={`system-preset-${preset.key}`}
            data-status={preset.status}
            style={{ display: "grid", gap: 8, padding: "10px 0" }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <strong className="mono">{preset.label}</strong>
              <Chip tone="blue" icon="◈">{t("create.system.chip")}</Chip>
              <Chip tone={STATUS_TONE[preset.status]}>
                {t(`create.system.status.${preset.status}`)}
              </Chip>
              {preset.update_available && (
                <Chip tone="amber">{t("create.system.updateAvailable")}</Chip>
              )}
              <span className="mono dim" style={{ fontSize: 11 }}>
                harnessName: {preset.name.replace(/-/g, "_")} · skill v{preset.skill_version}
                {installed && preset.installed_skill_version && (
                  <> · {t("create.system.installedVersion", { v: preset.installed_skill_version })}</>
                )}
              </span>
            </div>
            <p className="dim" style={{ margin: 0 }} data-testid="preset-description">
              {t(`create.system.presets.${preset.key}.description`, preset.description)}
            </p>
            <div className="mono dim" style={{ fontSize: 11 }}>
              {t("create.system.tools")}: {preset.allowed_tools.join(", ")}
              {` · ${t("create.system.memoryDisabled")}`}
              {preset.model_id ? ` · ${t("create.system.model")}: ${preset.model_id}` : ""}
              {preset.knowledge_bases.length > 0
                ? ` · ${t("create.system.kb", { n: preset.knowledge_bases.length })}`
                : ` · ${t("create.system.kbNone")}`}
            </div>
            <div className="dim" style={{ fontSize: 11 }}>{t("create.system.adminOptionsApiOnly")}</div>
            {preset.error && preset.status === "failed" && (
              <div className="note" style={{ borderColor: "var(--crit)" }} data-testid="preset-error">
                <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                <span className="mono" style={{ fontSize: 11 }}>{preset.error}</span>
              </div>
            )}
            {preset.operation && (
              <div
                className="note"
                style={
                  preset.operation.job_status === "failed" ? { borderColor: "var(--crit)" } : undefined
                }
                data-testid="preset-operation"
                data-job-status={preset.operation.job_status}
              >
                <span className="i">{preset.operation.job_status === "failed" ? "[✕]" : "[⟳]"}</span>
                <span>
                  {t(
                    preset.operation.job_status === "failed"
                      ? "create.system.uninstallFailed"
                      : "create.system.uninstallRunning",
                    { attempt: preset.operation.attempt, job: preset.operation.job_id.slice(0, 8) },
                  )}
                  {preset.operation.error && (
                    <>
                      {" "}
                      <span className="mono" style={{ fontSize: 11 }}>
                        {preset.operation.error}
                      </span>
                    </>
                  )}
                </span>
              </div>
            )}
            {blockers && (
              <div className="note" data-testid="preset-reason">
                <span className="i">[i]</span>
                <span>
                  {t(installed ? "create.system.requirementsInstalled" : "create.system.requirements", {
                    list: blockers,
                  })}
                </span>
              </div>
            )}
            {collision && (
              <div className="note" data-testid="preset-collision">
                <span className="i">[i]</span>
                <span>{collision}</span>
              </div>
            )}
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
              {preset.status === "active" && preset.key === "aws-agent-solution-architect" && (
                <Link
                  className="assist-link"
                  style={{ marginTop: 0 }}
                  to="/create/assistant"
                  data-testid="preset-open-assistant"
                >
                  {t("create.system.openAssistant")}
                </Link>
              )}
              {!installed && (
                <Btn
                  primary
                  data-testid={`install-${preset.key}`}
                  disabled={!preset.can_install || isBusy}
                  disabledReason={isAdmin ? (blockers ?? collision ?? undefined) : undefined}
                  title={adminHint}
                  onClick={() => setConfirm({ kind: "install", preset })}
                >
                  {t("create.system.install")}
                </Btn>
              )}
              {installed && preset.status !== "deploying" && preset.status !== "uninstalling" && (
                <Btn
                  data-testid={`repair-${preset.key}`}
                  disabled={!preset.can_repair || isBusy}
                  disabledReason={isAdmin ? (blockers ?? undefined) : undefined}
                  title={adminHint}
                  onClick={() => setConfirm({ kind: "repair", preset })}
                >
                  {t(preset.update_available ? "create.system.update" : "create.system.repair")}
                </Btn>
              )}
              {installed && preset.status === "active" && preset.agent_id && (
                <Link className="btn" to={`/chat?agent=${preset.agent_id}`}>
                  {t("create.list.chat")}
                </Link>
              )}
              {installed && preset.agent_id && onDetails && (
                <Btn
                  data-testid={`details-${preset.key}`}
                  onClick={() => onDetails(preset.agent_id as string)}
                >
                  {t("create.list.details")}
                </Btn>
              )}
              {installed && preset.status !== "deploying" && (
                <Btn
                  data-testid={`uninstall-${preset.key}`}
                  disabled={!preset.can_uninstall || isBusy}
                  title={adminHint}
                  onClick={() => setConfirm({ kind: "uninstall", preset })}
                >
                  {t(
                    preset.operation?.retryable
                      ? "create.system.retryUninstall"
                      : "create.system.uninstall",
                  )}
                </Btn>
              )}
              {!isAdmin && (
                <span className="dim mono" style={{ fontSize: 11 }} data-testid="preset-admin-only">
                  {t("create.system.adminOnly")}
                </span>
              )}
            </div>
          </div>
        );
      })}
      <ConfirmDialog
        open={confirm != null}
        title={confirm ? t(`create.system.confirm.${confirm.kind}Title`) : ""}
        body={confirm ? t(`create.system.confirm.${confirm.kind}`, { name: confirm.preset.label }) : ""}
        confirmLabel={confirm ? t(`create.system.${confirm.kind}`) : ""}
        onConfirm={() => {
          if (confirm) void run(confirm.kind, confirm.preset);
          setConfirm(null);
        }}
        onCancel={() => setConfirm(null)}
      />
    </Panel>
  );
}
