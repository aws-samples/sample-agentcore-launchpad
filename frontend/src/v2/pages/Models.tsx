import { CheckCircle2, RotateCcw, XCircle } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../auth/auth-context";
import { api, errorMessage, type ModelProbeCheck, type ModelProbeResult, type ModelRoleSetting } from "../../lib/api";
import { ACTOR_MODELS } from "../../lib/evaluation";
import { JUDGE_MODEL_OPTIONS } from "../../lib/evaluators";
import { reloadModelDefaults, roleAccepts, roleModelOptions, STATIC_MODEL_DEFAULTS } from "../../lib/model-defaults";
import { MODEL_CATALOG } from "../../lib/models";
import { fmtTime } from "../format";
import { useLoad, useV2Toast } from "../hooks";
import { Alert, Button, Card, type Column, Confirm, PageHeader, Select, Table, Tag } from "../ui";

/** Sentinel option revealing the free-text model-id input. */
const CUSTOM = "__custom__";
/** The `Select` value of the "none" option of a role that allows empty. */
const NONE = "__none__";

/** Every Bedrock (Converse) id the console already offers somewhere: the agent
 *  catalog, the judge and actor lists, and the built-in role defaults. */
const CATALOG_IDS: string[] = [
  ...new Set([
    ...MODEL_CATALOG.bedrock.map((m) => m.model_id),
    ...JUDGE_MODEL_OPTIONS,
    ...ACTOR_MODELS,
    ...Object.values(STATIC_MODEL_DEFAULTS).filter(Boolean),
  ]),
];
const LABELS = new Map(MODEL_CATALOG.bedrock.map((m) => [m.model_id, m.label]));

type Probe = { busy: boolean; results: ModelProbeResult[]; error: string | null };
const IDLE: Probe = { busy: false, results: [], error: null };

function ProbeView({ probe }: { probe: Probe }) {
  const { t } = useTranslation();
  if (probe.busy) return <span className="v2-muted">{t("v2.models.probing")}</span>;
  if (probe.error) return <Alert tone="error">{probe.error}</Alert>;
  if (!probe.results.length) return null;
  return (
    <div className="v2-stack" style={{ gap: 4 }} data-testid="v2-models-probe-result">
      {probe.results.map((r) => (
        <span key={r.check} className="v2-row" style={{ gap: 6, alignItems: "flex-start" }}>
          {r.ok ? (
            <CheckCircle2 size={14} aria-hidden="true" style={{ color: "var(--v2-success)" }} />
          ) : (
            <XCircle size={14} aria-hidden="true" style={{ color: "var(--v2-danger)" }} />
          )}
          <span>
            <b>{t(`v2.models.check.${r.check}`)}</b>
            {" · "}
            {r.ok
              ? t("v2.models.probeOk", { ms: r.latency_ms, region: r.region })
              : t("v2.models.probeFail", { code: r.error_code ?? "Error", message: r.message ?? "", region: r.region })}
          </span>
        </span>
      ))}
    </div>
  );
}

/** One role's editor: pick (or type) a model, probe it, save it, or reset the override. */
function RoleEditor({ role, onSaved }: { role: ModelRoleSetting; onSaved: (row: ModelRoleSetting) => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const options = roleModelOptions(role, CATALOG_IDS);
  const [draft, setDraft] = useState(role.model_id);
  const [custom, setCustom] = useState(!!role.model_id && !options.includes(role.model_id));
  const [confirm, setConfirm] = useState<"save" | "reset" | null>(null);
  const [busy, setBusy] = useState(false);
  const [probe, setProbe] = useState<Probe>(IDLE);
  const name = t(`v2.models.role.${role.role}.name`, { defaultValue: role.role });
  const value = draft.trim();
  const valid = roleAccepts(role, value);
  const changed = value !== role.model_id;

  const write = async (kind: "save" | "reset") => {
    setBusy(true);
    try {
      const row = kind === "save" ? await api.adminSetModel(role.role, value) : await api.adminResetModel(role.role);
      setConfirm(null);
      toast("success", t(kind === "save" ? "v2.models.saved" : "v2.models.resetDone", { role: name }));
      // the pickers' module cache: new forms pre-select the new value at once
      void reloadModelDefaults();
      onSaved(row);
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const runProbe = async () => {
    // tool use matters only to a role that runs tools
    const checks: ModelProbeCheck[] = role.needs_tools ? ["converse", "tool_use"] : ["converse"];
    setProbe({ ...IDLE, busy: true });
    try {
      const results = await Promise.all(checks.map((check) => api.adminProbeModel(value, check)));
      setProbe({ ...IDLE, results });
    } catch (err) {
      setProbe({ ...IDLE, error: errorMessage(err) });
    }
  };

  return (
    <div className="v2-stack" data-testid={`v2-models-editor-${role.role}`}>
      <Select
        value={custom ? CUSTOM : value || (role.allow_empty ? NONE : "")}
        mono
        ariaLabel={t("v2.models.pick", { role: name })}
        testId={`v2-models-select-${role.role}`}
        onChange={(v) => {
          setProbe(IDLE);
          if (v === CUSTOM) {
            setCustom(true);
            return;
          }
          setCustom(false);
          setDraft(v === NONE ? "" : v);
        }}
        options={[
          ...(role.allow_empty ? [{ value: NONE, label: t("v2.models.none") }] : []),
          ...options.map((id) => ({
            value: id,
            label: `${LABELS.has(id) ? `${LABELS.get(id)} · ` : ""}${id}${id === role.default ? ` · ${t("v2.models.isDefault")}` : ""}`,
          })),
          { value: CUSTOM, label: t("v2.models.custom") },
        ]}
      />
      {custom && (
        <input
          className="v2-input mono"
          aria-label={t("v2.models.custom")}
          value={draft}
          placeholder={t("v2.models.customPlaceholder", { example: role.default || role.model_id })}
          onChange={(e) => {
            setProbe(IDLE);
            setDraft(e.target.value);
          }}
          data-testid={`v2-models-custom-${role.role}`}
        />
      )}
      {value && !valid && <span className="v2-muted" style={{ color: "var(--v2-danger)" }}>{t("v2.models.invalid")}</span>}
      <div className="v2-row">
        <Button size="sm" disabled={probe.busy || !value || !valid} onClick={() => void runProbe()} testId={`v2-models-probe-${role.role}`}>
          {t("v2.models.probe")}
        </Button>
        <Button
          size="sm"
          kind="primary"
          disabled={busy || !changed || !valid}
          onClick={() => setConfirm("save")}
          testId={`v2-models-save-${role.role}`}
        >
          {t("v2.models.save")}
        </Button>
        {role.overridden && (
          <Button size="sm" disabled={busy} title={t("v2.models.resetHint")} onClick={() => setConfirm("reset")} testId={`v2-models-reset-${role.role}`}>
            <RotateCcw size={13} aria-hidden="true" /> {t("v2.models.reset")}
          </Button>
        )}
      </div>
      <ProbeView probe={probe} />
      <Confirm
        open={confirm !== null}
        title={t(confirm === "reset" ? "v2.models.confirmResetTitle" : "v2.models.confirmSaveTitle", { role: name })}
        body={
          <div className="v2-stack">
            <span>
              {confirm === "reset"
                ? t("v2.models.confirmResetBody", { role: name, model: role.default || t("v2.models.none") })
                : t("v2.models.confirmSaveBody", { role: name, model: value || t("v2.models.none") })}
            </span>
            <span className="v2-muted">{t("v2.models.confirmGlobal")}</span>
            {(role.role === "judge_model_id" || role.role === "judge_fallback_model_id") && (
              <Alert tone="warn">{t("v2.models.noteJudge")}</Alert>
            )}
          </div>
        }
        confirmLabel={t(confirm === "reset" ? "v2.models.reset" : "v2.models.save")}
        busy={busy}
        onConfirm={() => confirm && void write(confirm)}
        onClose={() => setConfirm(null)}
      />
    </div>
  );
}

function ModelsManager() {
  const { t } = useTranslation();
  const loaded = useLoad(() => api.modelDefaults(), "v2-model-defaults");
  // rows replaced by a save / reset, over the loaded list
  const [saved, setSaved] = useState<Record<string, ModelRoleSetting>>({});
  const rows = (loaded.data?.roles ?? []).map((row) => saved[row.role] ?? row);

  const columns: Column<ModelRoleSetting>[] = [
    {
      key: "role",
      title: t("v2.models.col.role"),
      width: "30%",
      render: (r) => (
        <div className="v2-stack" style={{ gap: 2 }}>
          <b>{t(`v2.models.role.${r.role}.name`, { defaultValue: r.role })}</b>
          <span className="v2-muted">{t(`v2.models.role.${r.role}.desc`, { defaultValue: "" })}</span>
          <span className="mono v2-muted">{r.role}</span>
        </div>
      ),
    },
    {
      key: "current",
      title: t("v2.models.col.current"),
      width: "32%",
      render: (r) => (
        <div className="v2-stack" style={{ gap: 4 }}>
          <span className="mono" data-testid={`v2-models-current-${r.role}`}>{r.model_id || t("v2.models.none")}</span>
          <span className="v2-row">
            {r.overridden ? (
              <Tag tone="orange" testId={`v2-models-overridden-${r.role}`}>{t("v2.models.overridden")}</Tag>
            ) : (
              <Tag tone="gray">{t("v2.models.isDefault")}</Tag>
            )}
            {r.constraint && <Tag tone="outline">{t("v2.models.familyOnly", { families: r.constraint.map(familyLabel).join(" / ") })}</Tag>}
            {r.needs_tools && <Tag tone="outline">{t("v2.models.needsTools")}</Tag>}
          </span>
          {r.overridden && (
            <>
              <span className="v2-muted">
                {t("v2.models.defaultValue")} <span className="mono">{r.default || t("v2.models.none")}</span>
              </span>
              <span className="v2-muted">{t("v2.models.updated", { who: r.updated_by ?? "—", when: fmtTime(r.updated_at) })}</span>
            </>
          )}
        </div>
      ),
    },
    {
      key: "change",
      title: t("v2.models.col.change"),
      render: (r) => (
        <RoleEditor
          // a save / reset remounts the editor on its new value
          key={`${r.role}:${r.model_id}:${r.overridden}`}
          role={r}
          onSaved={(row) => setSaved((prev) => ({ ...prev, [row.role]: row }))}
        />
      ),
    },
  ];

  return (
    <>
      <PageHeader title={t("v2.models.title")} desc={t("v2.models.desc")} />
      <Alert tone="warn">
        <div className="v2-stack" style={{ gap: 4 }} data-testid="v2-models-notes">
          <span>{t("v2.models.noteGlobal")}</span>
          <span>{t("v2.models.noteJudge")}</span>
          <span>{t("v2.models.noteStored")}</span>
        </div>
      </Alert>
      <Card title={t("v2.models.roles")} flush testId="v2-models">
        <Table
          columns={columns}
          rows={rows}
          rowKey={(r) => r.role}
          loading={loaded.loading}
          error={loaded.error}
          onRetry={loaded.reload}
          testId="v2-models-table"
        />
      </Card>
    </>
  );
}

/**
 * 模型 — admin-only: every model the platform itself calls or pre-selects (the agent
 * default, the evaluation judge, the assistant, the architect preset, the simulated
 * user, the Studio fallback, Skill Lab, …), its effective value, a one-call Converse
 * probe in the hub's default workspace, and a console override (applied at once to
 * every workspace and member of this deployment, persisted in its ledger).
 */
// display names for the provider families a role accepts
const FAMILY_LABEL: Record<string, string> = { anthropic: "Anthropic", openai: "OpenAI" };
const familyLabel = (f: string) => FAMILY_LABEL[f] ?? f;

export function V2Models() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  // Members never fire the admin page's requests (and never see the editors).
  if (!isAdmin) {
    return (
      <>
        <PageHeader title={t("v2.models.title")} />
        <Card testId="v2-models-forbidden">
          <Alert tone="warn">{t("v2.models.forbiddenBody")}</Alert>
        </Card>
      </>
    );
  }
  return <ModelsManager />;
}
