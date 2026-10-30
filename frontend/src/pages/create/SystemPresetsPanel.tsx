import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { Btn, Chip, ConfirmDialog, Panel, useToast } from "../../components";
import type { ChipTone } from "../../components";
import type {
  JobInfo,
  SystemPresetInfo,
  SystemPresetSettings,
  SystemPresetStatus,
} from "../../lib/api";
import { api, ApiError } from "../../lib/api";
import { useWorkspace } from "../../workspace/workspace-context";
import { presetConfigureRequest } from "./presetSettings";
import type { PresetConfigureRequest } from "./presetSettings";
import { zonedClock } from "../../lib/timestamps";

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

/** `repair` = the card's "Repair snapshot" (N17-obs234): the same bodiless install route —
 *  the server re-publishes because the pinned model catalog is behind the ledger's stamp. */
type Operation = "install" | "uninstall" | "repair";

/**
 * The preset's current/last job (install, repair, uninstall) with an on-demand log —
 * N12-obs31 (a): a preset job can be filed under a workspace id `GET /api/jobs/{id}`
 * does not see, so the log is read through the preset's own admin route. Polls while
 * the job is live and the log is open.
 */
function PresetJobLog({
  preset,
  workspaceId,
  isAdmin,
}: {
  preset: SystemPresetInfo;
  workspaceId: string | null;
  isAdmin: boolean;
}) {
  const { t } = useTranslation();
  const job = preset.last_job ?? null;
  const [open, setOpen] = useState(false);
  const [log, setLog] = useState<JobInfo | null>(null);
  // the raw failure (code + fallback message); translated in render so a language
  // switch re-renders without re-fetching the log
  const [failed, setFailed] = useState<{ code: string | null; message: string } | null>(null);
  const live = job?.status === "queued" || job?.status === "running";
  const jobId = job?.id ?? null;
  useEffect(() => {
    if (!open || !jobId) return;
    let alive = true;
    const read = () =>
      void api
        .getSystemPresetJob(preset.key, jobId, workspaceId)
        .then((res) => {
          if (!alive) return;
          setLog(res);
          setFailed(null);
        })
        .catch((err: unknown) => {
          if (!alive) return;
          setFailed(
            err instanceof ApiError ? { code: err.code, message: err.message } : { code: null, message: String(err) },
          );
        });
    read();
    const timer = live ? window.setInterval(read, POLL_MS) : undefined;
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [open, jobId, live, preset.key, workspaceId]);
  if (!job) return null;
  return (
    <div className="mono dim" style={{ fontSize: 11 }} data-testid="preset-job" data-job-status={job.status}>
      {t("create.system.job.label", {
        kind: t(`create.system.job.kind.${job.type}`, job.type),
        job: job.id.slice(0, 8),
        status: job.status,
      })}
      {isAdmin && (
        <>
          {" · "}
          <button
            type="button"
            className="rowact"
            data-testid="preset-job-log-toggle"
            onClick={() => setOpen((v) => !v)}
          >
            {t(open ? "create.system.job.hideLog" : "create.system.job.viewLog")}
          </button>
        </>
      )}
      {open && failed && (
        <div style={{ color: "var(--crit)" }}>
          {t("create.system.job.loadFailed", {
            reason: failed.code ? t(`apiErrors.${failed.code}`, failed.message) : failed.message,
          })}
        </div>
      )}
      {open && log && (
        <pre
          data-testid="preset-job-log"
          style={{ margin: "6px 0 0", maxHeight: 220, overflow: "auto", whiteSpace: "pre-wrap" }}
        >
          {log.events.length === 0
            ? t("create.system.job.empty")
            : log.events
                .map((e) => `${zonedClock(e.ts)} ${e.stage.padEnd(9)} ${e.level === "error" ? "✕ " : ""}${e.msg}`)
                .join("\n")}
        </pre>
      )}
    </div>
  );
}

/**
 * System-managed presets: platform-owned agents an administrator installs on
 * purpose. Reads are ledger-only (`GET /api/system-agents` builds no AWS client),
 * so this panel may poll while a preset deploys; install/uninstall are the only
 * paths this panel drives that reach AWS, and the server refuses them for
 * non-administrators regardless of what this component renders. Changing or
 * re-publishing an installed preset (including the retry of a failed deploy) is
 * the parent's shared agent editor — CONFIGURE hands the row over through
 * `onConfigure`; the preset's Skill record is managed in the Registry.
 *
 * Every asynchronous outcome is checked against the workspace it was started in
 * and against the mounted state, so an operation finishing after a workspace
 * switch (or after navigation) never announces into the wrong context.
 */
export function SystemPresetsPanel({
  onChanged,
  onDetails,
  onConfigure,
}: {
  /** fired after an install/uninstall changed ledger state AND when a deploying
   * preset reaches a terminal status, so the parent list converges */
  onChanged?: () => void;
  /** open the deployment detail for the preset's agent */
  onDetails?: (agentId: string) => void;
  /** open the shared agent editor on the preset's stored settings (edit / review) */
  onConfigure?: (req: PresetConfigureRequest) => void;
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
      .listSystemPresets(startedIn) // pinned: never the shared selection of another tab
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
        const res = await api.uninstallSystemPreset(preset.key, startedIn);
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
        // a first install with the preset defaults, or the "Repair snapshot" action of an
        // active preset whose catalog pin is behind (`snapshot_behind`): the body stays `{}`
        // in both cases — the SERVER decides to re-publish (N17-obs234); every settings
        // change or forced re-publish goes through the shared editor (onConfigure)
        const res = await api.installSystemPreset(preset.key, {}, startedIn);
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
  // CONFIGURE / VIEW SETTINGS: the shared editor decides nothing about permissions —
  // the verdict travels with the row (administrator + settled + prerequisites), and
  // the server refuses a member's POST regardless.
  const configure = (preset: SystemPresetInfo) =>
    onConfigure?.(presetConfigureRequest(preset, scope.current, isAdmin, t));

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
        // N17-obs234: an active preset whose installed catalog pin is behind the ledger's
        // stamp (the model-providers banner) offers the repair right on its card
        const snapshotBehind =
          installed && preset.status === "active" && preset.snapshot_behind === true;
        // N17-obs202: an active preset that FOLLOWS the agent-default role whose installed
        // model lags the role (the administrator moved the model default) shows a note; the
        // same bodiless repair applies the new model — one button for both notes
        const defaultChanged =
          installed && preset.status === "active" && preset.default_changed === true;
        const repairable = snapshotBehind || defaultChanged;
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
                {preset.status === "deploying" && preset.last_job?.trigger === "auto"
                  ? t("create.system.autoRepairing") // N17-obs206: the sweep's own repair job
                  : t(`create.system.status.${preset.status}`)}
              </Chip>
              {preset.update_available && (
                <Chip tone="amber">{t("create.system.updateAvailable")}</Chip>
              )}
              <span className="mono dim" style={{ fontSize: 11 }} data-testid="preset-release">
                harnessName: {preset.name.replace(/-/g, "_")} · skill v{preset.skill_version}
                {installed && preset.installed_skill_version && (
                  <> · {t("create.system.installedVersion", { v: preset.installed_skill_version })}</>
                )}
                {preset.release && <> · {t("create.system.release", { tag: preset.release.tag })}</>}
                {installed && preset.installed_release && preset.installed_release !== preset.release?.tag && (
                  <> · {t("create.system.installedRelease", { tag: preset.installed_release })}</>
                )}
                {installed && preset.version && (
                  <> · {t("create.system.runtimeVersion", { v: preset.version })}</>
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
            {installed && "model_id" in preset.settings && (
              <div className="mono dim" style={{ fontSize: 11 }} data-testid="preset-inference">
                {t("create.system.settings.summary", {
                  tokens:
                    (preset.settings as SystemPresetSettings).max_tokens ??
                    t("create.system.settings.providerDefault"),
                  effort:
                    (preset.settings as SystemPresetSettings).reasoning_effort ??
                    t("create.system.settings.effortNone"),
                  iterations: (preset.settings as SystemPresetSettings).max_iterations,
                  timeout: (preset.settings as SystemPresetSettings).timeout_seconds,
                })}
              </div>
            )}
            <div className="dim" style={{ fontSize: 11 }}>{t("create.system.adminOptionsApiOnly")}</div>
            <PresetJobLog preset={preset} workspaceId={workspaceId} isAdmin={isAdmin} />
            {preset.release_error && (
              <div className="note" style={{ borderColor: "var(--crit)" }} data-testid="preset-release-error">
                <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                <span className="mono" style={{ fontSize: 11 }}>
                  {t("create.system.releaseError")} {preset.release_error}
                </span>
              </div>
            )}
            {preset.error && (preset.status === "failed" || preset.last_deploy_failed) && (
              <div className="note" style={{ borderColor: "var(--crit)" }} data-testid="preset-error">
                <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                <span className="mono" style={{ fontSize: 11 }}>
                  {preset.last_deploy_failed ? `${t("v2.agents.lastDeployFailedDetail")} ` : ""}
                  {preset.error}
                </span>
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
            {snapshotBehind && (
              <div className="note" data-testid="preset-snapshot-behind">
                <span className="i">[i]</span>
                <span>
                  {t("create.system.snapshotBehind", {
                    installed: preset.catalog_version?.installed ?? "?",
                    current: preset.catalog_version?.current ?? "?",
                  })}
                </span>
              </div>
            )}
            {defaultChanged && (
              <div className="note" data-testid="preset-default-changed">
                <span className="i">[i]</span>
                <span>
                  {t("create.system.defaultChanged", {
                    installed: preset.model_id ?? "?",
                    current: preset.defaults.model_id,
                  })}
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
              {repairable && (
                <Btn
                  primary
                  data-testid={`repair-snapshot-${preset.key}`}
                  disabled={!preset.can_repair || isBusy}
                  disabledReason={isAdmin ? (blockers ?? undefined) : undefined}
                  title={
                    adminHint ??
                    t(snapshotBehind ? "create.system.repairSnapshotHint" : "create.system.repairDefaultHint")
                  }
                  onClick={() => void run("repair", preset)}
                >
                  {t("create.system.repairSnapshot")}
                </Btn>
              )}
              {installed && "model_id" in preset.settings && onConfigure && (
                <Btn
                  data-testid={`settings-${preset.key}`}
                  disabled={isBusy}
                  title={
                    isAdmin
                      ? t("create.system.settings.configureHint")
                      : t("create.system.settings.readOnly")
                  }
                  onClick={() => configure(preset)}
                >
                  {t(
                    preset.can_configure
                      ? "create.system.settings.configure"
                      : "create.system.settings.view",
                  )}
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
