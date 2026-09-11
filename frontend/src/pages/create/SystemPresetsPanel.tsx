import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { Btn, Chip, ConfirmDialog, Panel, useToast } from "../../components";
import type { ChipTone } from "../../components";
import type { SystemPresetInfo, SystemPresetStatus } from "../../lib/api";
import { api, ApiError } from "../../lib/api";

const STATUS_TONE: Record<SystemPresetStatus, ChipTone> = {
  configuration_required: "muted",
  not_installed: "muted",
  deploying: "warn",
  active: "good",
  failed: "crit",
};

/**
 * System-managed presets: platform-owned agents an administrator installs on
 * purpose. Reads are ledger-only (`GET /api/system-agents` builds no AWS client),
 * so this panel may poll while a preset deploys; the install button is the one
 * place a preset reaches AWS, and it is rendered only for administrators — the
 * server refuses the call for everyone else regardless.
 */
export function SystemPresetsPanel({
  onChanged,
  onDetails,
}: {
  /** fired after an install/repair/uninstall changed ledger state */
  onChanged?: () => void;
  /** open the deployment detail for the preset's agent */
  onDetails?: (agentId: string) => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const { isAdmin } = useAuth();
  const [presets, setPresets] = useState<SystemPresetInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<
    { kind: "install" | "repair" | "uninstall"; preset: SystemPresetInfo } | null
  >(null);

  const load = useCallback(() => {
    void api
      .listSystemPresets()
      .then((res) => {
        setPresets(res.presets);
        setError(null);
      })
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? t(`apiErrors.${err.code}`, err.message) : String(err));
      });
  }, [t]);

  useEffect(() => load(), [load]);
  // a deploying preset resolves in ~30 s; poll the ledger read until it settles
  const deploying = (presets ?? []).some((p) => p.status === "deploying");
  useEffect(() => {
    if (!deploying) return;
    const timer = window.setInterval(load, 4000);
    return () => window.clearInterval(timer);
  }, [deploying, load]);

  const run = async (kind: "install" | "repair" | "uninstall", preset: SystemPresetInfo) => {
    setBusy(preset.key);
    try {
      if (kind === "uninstall") {
        await api.uninstallSystemPreset(preset.key);
        toast(t("create.system.uninstalled", { name: preset.label }));
      } else {
        const res = await api.installSystemPreset(preset.key, kind === "repair" ? { force: true } : {});
        toast(
          t(
            res.changed
              ? res.created
                ? "create.system.installStarted"
                : "create.system.repairStarted"
              : "create.system.alreadyCurrent",
            { name: preset.label },
          ),
        );
      }
      load();
      onChanged?.();
    } catch (err) {
      toast(err instanceof ApiError ? t(`apiErrors.${err.code}`, err.message) : String(err));
    } finally {
      setBusy(null);
    }
  };

  const adminHint = isAdmin ? undefined : t("create.system.adminOnly");

  return (
    <Panel title={t("create.system.title")} sub={t("create.system.sub")} className="system-presets">
      {error && (
        <div className="note" style={{ borderColor: "var(--crit)" }}>
          <span className="i">[!]</span>
          <span>{error}</span>
        </div>
      )}
      {presets?.length === 0 && <div className="dim mono">{t("create.system.empty")}</div>}
      {(presets ?? []).map((preset) => {
        const installed = preset.agent_id != null;
        const reason =
          preset.status === "configuration_required"
            ? t("create.system.requirements", { list: preset.requirements.join("; ") })
            : preset.name_collision
              ? t("create.system.collision", { name: preset.name_collision.agent_name })
              : null;
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
            <p className="dim" style={{ margin: 0 }}>{preset.description}</p>
            <div className="mono dim" style={{ fontSize: 11 }}>
              {t("create.system.tools")}: {preset.allowed_tools.join(", ")}
              {preset.model_id ? ` · ${t("create.system.model")}: ${preset.model_id}` : ""}
              {preset.knowledge_bases.length > 0
                ? ` · ${t("create.system.kb", { n: preset.knowledge_bases.length })}`
                : ` · ${t("create.system.kbNone")}`}
            </div>
            {preset.error && preset.status === "failed" && (
              <div className="note" style={{ borderColor: "var(--crit)" }} data-testid="preset-error">
                <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                <span className="mono" style={{ fontSize: 11 }}>{preset.error}</span>
              </div>
            )}
            {reason && (
              <div className="note" data-testid="preset-reason">
                <span className="i">[i]</span>
                <span>{reason}</span>
              </div>
            )}
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
              {!installed && (
                <Btn
                  primary
                  data-testid={`install-${preset.key}`}
                  disabled={!isAdmin || !preset.can_install || busy === preset.key}
                  disabledReason={isAdmin ? (reason ?? undefined) : undefined}
                  title={adminHint}
                  onClick={() => setConfirm({ kind: "install", preset })}
                >
                  {t("create.system.install")}
                </Btn>
              )}
              {installed && preset.status !== "deploying" && (
                <Btn
                  data-testid={`repair-${preset.key}`}
                  disabled={!isAdmin || busy === preset.key}
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
                <Btn onClick={() => onDetails(preset.agent_id as string)}>
                  {t("create.list.details")}
                </Btn>
              )}
              {installed && preset.status !== "deploying" && (
                <Btn
                  data-testid={`uninstall-${preset.key}`}
                  disabled={!isAdmin || busy === preset.key}
                  title={adminHint}
                  onClick={() => setConfirm({ kind: "uninstall", preset })}
                >
                  {t("create.system.uninstall")}
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
