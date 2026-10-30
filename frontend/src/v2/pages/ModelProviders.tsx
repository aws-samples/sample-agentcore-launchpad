import { useCallback, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import type { ModelProviderOut, ModelRowOut } from "../../lib/api";
import { api } from "../../lib/api";
import { useWorkspace } from "../../workspace/workspace-context";
import { useLoad } from "../hooks";
import { useMayMutate } from "../ownership";
import { Alert, Button, Card, PageHeader, SubTabs } from "../ui";
import { useModelProviderActions } from "./modelProviders/actions";
import { Changes } from "./modelProviders/Changes";
import { isView, MEMBER_VIEWS, providerOf, type View, VIEWS } from "./modelProviders/common";
import { ModelDrawer } from "./modelProviders/ModelDrawer";
import { ModelTable } from "./modelProviders/ModelTable";
import { ModelProbeDialog, ProviderProbeDialog } from "./modelProviders/ProbeDialog";
import { ProviderCards } from "./modelProviders/ProviderCards";
import { ProviderDrawer } from "./modelProviders/ProviderDrawer";
import { RolesTab } from "./modelProviders/RolesTab";
import { WorkspaceMatrix } from "./modelProviders/WorkspaceMatrix";
import "./modelProviders/modelProviders.css";

/**
 * Settings → Model Providers (D21 → N17-obs203) — the registry of model sources and the
 * models they serve. **Owner-bound since N17-obs203 (user decision 2026-10-21):** every
 * signed-in member registers their OWN providers (`scope: personal`, visible to them and to
 * administrators), edits / probes / deletes them and adds their models; an administrator
 * sees everything, shares a provider with the workspace (`scope: workspace`) and may change
 * or delete members' rows. The server decides (404 for a hidden row, 403 `model_provider.
 * forbidden` for another creator's); the console mirrors the rule with `useMayMutate` so a
 * member never runs into the 403. Views: provider cards (`?view=providers`, `?provider=<id>`
 * selects one and shows its model table) for everyone; the platform's model roles
 * (`?view=roles`, r15-e), the workspace × model enable matrix (`?view=workspaces`) and the
 * change journal (`?view=changes`) for administrators. Credentials are references (SSM
 * parameter names / Identity provider names) — no secret is ever rendered here (§9).
 */
/** `detail.preset_key` of a `preset_snapshot_behind` warning (`catalog_snapshot_warnings`), else null. */
function warningPresetKey(detail: unknown): string | null {
  if (detail && typeof detail === "object" && "preset_key" in detail) {
    const key = (detail as { preset_key?: unknown }).preset_key;
    return typeof key === "string" && key ? key : null;
  }
  return null;
}

/** `detail.workspace_id` of a `preset_snapshot_behind` warning, else null (N17-obs235). */
function warningWorkspaceId(detail: unknown): string | null {
  if (detail && typeof detail === "object" && "workspace_id" in detail) {
    const id = (detail as { workspace_id?: unknown }).workspace_id;
    return typeof id === "string" && id ? id : null;
  }
  return null;
}

export function V2ModelProviders() {
  return <ModelProvidersPage />;
}

type Dialog =
  | { kind: "addProvider" }
  | { kind: "editProvider"; provider: ModelProviderOut }
  | { kind: "probeProvider"; provider: ModelProviderOut }
  | { kind: "addModel"; provider: ModelProviderOut }
  | { kind: "editModel"; provider: ModelProviderOut; model: ModelRowOut }
  | { kind: "probeModel"; provider: ModelProviderOut; model: ModelRowOut }
  | null;

function ModelProvidersPage() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { workspaces: sessionWorkspaces, current: currentWorkspace, select: selectWorkspace } = useWorkspace();
  const navigate = useNavigate();
  const mayMutate = useMayMutate();
  const [params, setParams] = useSearchParams();
  const viewParam = params.get("view");
  // N17-obs203: a member's `?view=roles|workspaces|changes` deep link lands on the providers
  const views = isAdmin ? VIEWS : MEMBER_VIEWS;
  const view: View = isView(viewParam) && views.includes(viewParam) ? viewParam : "providers";
  const page = useLoad(() => api.modelProviders.list(), "model-providers");
  const reload = page.reload;
  const actions = useModelProviderActions(reload);
  const [dialog, setDialog] = useState<Dialog>(null);

  const providers = useMemo(() => page.data?.providers ?? [], [page.data]);
  const models = useMemo(() => [...(page.data?.models ?? [])].sort((a, b) => a.sort_order - b.sort_order), [page.data]);
  const requested = params.get("provider");
  const selected = providerOf(providers, requested) ?? providers[0] ?? null;
  const selectedModels = useMemo(() => (selected ? models.filter((m) => m.provider_id === selected.id) : []), [models, selected]);

  const setParam = (key: string, value: string | null) =>
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      if (value) next.set(key, value);
      else next.delete(key);
      return next;
    });

  const close = useCallback(() => setDialog(null), []);
  const saved = useCallback(() => {
    setDialog(null);
    reload();
  }, [reload]);
  // the provider probe stores its result server-side: refresh the cards once it answered
  const probed = useCallback(() => reload(), [reload]);

  const warnings = page.data?.warnings ?? [];
  // N17-obs235: the banner names the WORKSPACE the behind preset lives in — resolved to its
  // display name from the page's own flag matrix (administrators) or the session's workspace
  // list, the raw id when neither knows it
  const workspaceName = (id: string) =>
    page.data?.workspaces.find((w) => w.id === id)?.name ??
    sessionWorkspaces.find((w) => w.id === id)?.name ??
    id;
  return (
    <>
      <PageHeader
        title={t("nav.modelProviders")}
        desc={isAdmin ? t("modelProviders.meta", { version: page.data?.catalog_version ?? "—" }) : t("modelProviders.memberMeta")}
        tabs={
          // N17-obs251: members get the providers + roles (read-only) tabs; the rest stays admin
          <SubTabs
            value={view}
            tabs={views.map((value) => ({ value, label: t(`modelProviders.view.${value}`) }))}
            onChange={(value) => setParam("view", value === "providers" ? null : value)}
          />
        }
      />

      {page.error && (
        <div className="v2-mp-banners">
          <Alert tone="error">{t("modelProviders.loadFailed", { msg: page.error })}</Alert>
        </div>
      )}
      {warnings.length > 0 && (
        <div className="v2-mp-banners" data-testid="v2-mp-warnings">
          {warnings.map((w, i) => {
            // N17-obs150: one `preset_snapshot_behind` row per installed system preset — the
            // text names the preset from `detail.preset_key` (display name when known).
            const presetKey = warningPresetKey(w.detail);
            const preset = presetKey ? t(`modelProviders.presetName.${presetKey}`, presetKey) : undefined;
            const workspaceId = warningWorkspaceId(w.detail);
            const workspace = workspaceId ? workspaceName(workspaceId) : undefined;
            // N17-obs208: the banner carries the way to the fix — the presets panel
            // (`/agents/new`, "New agent → System presets") acts on the CURRENT workspace,
            // so a preset behind in another workspace switches first (the session's
            // `select`), and a workspace this session cannot select is named only
            const isPresetWarning = w.code === "preset_snapshot_behind";
            const sameWorkspace = !workspaceId || !currentWorkspace || currentWorkspace.id === workspaceId;
            const selectable = !!workspaceId && sessionWorkspaces.some((ws) => ws.id === workspaceId);
            const action = !isPresetWarning ? undefined : sameWorkspace ? (
              <Link to="/agents/new" className="v2-btn" data-testid={`v2-mp-warning-presets-${presetKey ?? i}-${workspaceId ?? ""}`}>
                {t("modelProviders.warningAction.openPresets")}
              </Link>
            ) : selectable ? (
              <Button
                size="sm"
                testId={`v2-mp-warning-presets-${presetKey ?? i}-${workspaceId}`}
                onClick={() => {
                  selectWorkspace(workspaceId);
                  navigate("/agents/new");
                }}
              >
                {t("modelProviders.warningAction.switchAndOpen", { workspace })}
              </Button>
            ) : (
              <span className="v2-muted" data-testid={`v2-mp-warning-presets-${presetKey ?? i}-${workspaceId}`}>
                {t("modelProviders.warningAction.switchFirst", { workspace })}
              </span>
            );
            return (
              <Alert key={`${w.code}-${presetKey ?? i}-${workspaceId ?? ""}`} tone="warn" action={action}>
                {t(`modelProviders.warning.${w.code}`, { defaultValue: w.message, preset, workspace })}
              </Alert>
            );
          })}
        </div>
      )}

      {view === "providers" && (
        <>
          <ProviderCards
            providers={providers}
            models={models}
            selectedId={selected?.id ?? null}
            onSelect={(id) => setParam("provider", id)}
            onAdd={() => setDialog({ kind: "addProvider" })}
            onEdit={(provider) => setDialog({ kind: "editProvider", provider })}
            onProbe={(provider) => setDialog({ kind: "probeProvider", provider })}
            onRefresh={reload}
            loading={page.loading}
            actions={actions}
            mayMutate={mayMutate}
          />
          {selected && (
            <Card title={t("modelProviders.modelsOf", { label: selected.label })} sub={selected.id_prefix ? t("modelProviders.idPrefix", { prefix: selected.id_prefix }) : undefined} testId="v2-mp-model-card">
              <ModelTable
                provider={selected}
                models={selectedModels}
                onAdd={() => setDialog({ kind: "addModel", provider: selected })}
                onEdit={(model) => setDialog({ kind: "editModel", provider: selected, model })}
                onProbe={(model) => setDialog({ kind: "probeModel", provider: selected, model })}
                loading={page.loading}
                actions={actions}
                readOnly={!mayMutate(selected)}
                canSetDefault={isAdmin}
              />
            </Card>
          )}
          {/* N17-obs203 §4: the selected provider's audit trail (administrators; the journal is theirs) */}
          {selected && isAdmin && <Changes key={selected.id} targetId={selected.id} compact />}
        </>
      )}
      {view === "roles" && (
        <RolesTab providers={providers} models={models} readOnly={!isAdmin} onProbe={(provider, model) => setDialog({ kind: "probeModel", provider, model })} />
      )}
      {view === "workspaces" && isAdmin && (
        <WorkspaceMatrix providers={providers} models={models} workspaces={page.data?.workspaces ?? []} actions={actions} />
      )}
      {view === "changes" && isAdmin && <Changes />}

      {actions.renderDialogs()}
      {dialog?.kind === "addProvider" && <ProviderDrawer open provider={null} onClose={close} onSaved={(p) => { saved(); setParam("provider", p.id); }} />}
      {dialog?.kind === "editProvider" && <ProviderDrawer key={dialog.provider.id} open provider={dialog.provider} onClose={close} onSaved={saved} />}
      {dialog?.kind === "probeProvider" && <ProviderProbeDialog provider={dialog.provider} onClose={close} onDone={probed} />}
      {dialog?.kind === "addModel" && <ModelDrawer open provider={dialog.provider} model={null} onClose={close} onSaved={saved} />}
      {dialog?.kind === "editModel" && <ModelDrawer key={dialog.model.id} open provider={dialog.provider} model={dialog.model} onClose={close} onSaved={saved} />}
      {dialog?.kind === "probeModel" && <ModelProbeDialog provider={dialog.provider} model={dialog.model} onClose={close} onApplied={reload} />}
    </>
  );
}
