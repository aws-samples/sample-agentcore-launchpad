import { Lock, Plus, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { ModelProviderOut, ModelRowOut } from "../../../lib/api";
import { fmtTime } from "../../format";
import type { Stamped } from "../../ownership";
import { Button, LinkButton, Tag } from "../../ui";
import type { useModelProviderActions } from "./actions";
import { credentialStatus, endpointHost, keyLast4, scopeTone } from "./common";
import { Switch } from "./Switch";

type Actions = ReturnType<typeof useModelProviderActions>;
type MayMutate = (row: Stamped | null | undefined) => boolean;

/**
 * One card per registered provider (plan §8 `?view=providers`): kind badge, endpoint
 * host, credential REFERENCE status (SSM parameter name / Identity provider — never a
 * value), enabled toggle, last probe, and the Probe / Register / Rotate / Edit / Delete
 * actions. Selecting a card shows its model table below.
 */
export function ProviderCards({
  providers,
  models,
  selectedId,
  onSelect,
  onAdd,
  onEdit,
  onProbe,
  onRefresh,
  loading,
  actions,
  mayMutate,
}: {
  providers: ModelProviderOut[];
  models: ModelRowOut[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onAdd: () => void;
  onEdit: (p: ModelProviderOut) => void;
  onProbe: (p: ModelProviderOut) => void;
  onRefresh: () => void;
  loading: boolean;
  actions: Actions;
  /** N17-obs203: the server's creator-or-admin rule, mirrored (`useMayMutate`) */
  mayMutate: MayMutate;
}) {
  const { t } = useTranslation();
  return (
    <>
      <div className="v2-toolbar">
        <Button onClick={onRefresh} disabled={loading} testId="v2-mp-refresh">
          <RefreshCw size={14} aria-hidden="true" />
          {t("v2.common.refresh")}
        </Button>
        <div className="end">
          <span className="v2-count">{t("modelProviders.count.providers", { count: providers.length })}</span>
          <Button kind="primary" onClick={onAdd} testId="v2-mp-add-provider">
            <Plus size={14} aria-hidden="true" />
            {t("modelProviders.action.addProvider")}
          </Button>
        </div>
      </div>
      <div className="v2-mp-providers" data-testid="v2-mp-providers">
        {providers.map((p) => (
          <ProviderCard
            key={p.id}
            provider={p}
            modelCount={models.filter((m) => m.provider_id === p.id).length}
            selected={p.id === selectedId}
            onSelect={() => onSelect(p.id)}
            onEdit={() => onEdit(p)}
            onProbe={() => onProbe(p)}
            actions={actions}
            mutable={mayMutate(p)}
          />
        ))}
        {providers.length === 0 && !loading && <div className="v2-muted">{t("modelProviders.empty.providers")}</div>}
      </div>
    </>
  );
}

function ProviderCard({
  provider: p,
  modelCount,
  selected,
  onSelect,
  onEdit,
  onProbe,
  actions,
  mutable,
}: {
  provider: ModelProviderOut;
  modelCount: number;
  selected: boolean;
  onSelect: () => void;
  onEdit: () => void;
  onProbe: () => void;
  actions: Actions;
  /** false = another creator's row: the card is read-only (the server would answer 403) */
  mutable: boolean;
}) {
  const { t } = useTranslation();
  const busy = actions.busyId === p.id;
  const cred = credentialStatus(p);
  const probe = p.probe;
  return (
    <div
      className={["v2-mp-card", selected ? "selected" : "", p.enabled ? "" : "off"].join(" ").trim()}
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
      data-testid={`v2-mp-provider-${p.name}`}
    >
      <div className="head">
        <b>{p.label}</b>
        <code>{p.name}</code>
        {p.builtin && (
          <span className="v2-mp-lock" title={t("modelProviders.builtinHint")}>
            <Lock size={12} aria-hidden="true" />
          </span>
        )}
        <div className="end" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
          <Tag tone={p.kind === "bedrock" ? "blue" : "outline"} testId={`v2-mp-kind-${p.name}`}>
            {t(`modelProviders.kind.${p.kind}`)}
          </Tag>
          {/* N17-obs203: scope badge — personal (owner + administrators) or workspace (shared) */}
          <Tag tone={scopeTone(p.scope)} title={t(`modelProviders.scopeHint.${p.scope}`)} testId={`v2-mp-scope-${p.name}`}>
            {t(`modelProviders.scope.${p.scope}`)}
            {p.owned ? ` · ${t("modelProviders.mine")}` : ""}
          </Tag>
          <Switch
            on={p.enabled}
            disabled={busy || !mutable}
            label={t(p.enabled ? "modelProviders.action.disable" : "modelProviders.action.enable")}
            onChange={() => actions.toggleProvider(p)}
            testId={`v2-mp-provider-toggle-${p.name}`}
          />
        </div>
      </div>
      <div className="meta">
        {p.created_by_name && (
          <div>
            <span className="k">{t("modelProviders.field.creator")}</span>
            <span data-testid={`v2-mp-creator-${p.name}`}>{p.created_by_name}</span>
          </div>
        )}
        <div>
          <span className="k">{t("modelProviders.field.endpoint")}</span>
          {p.kind === "bedrock" ? (
            <span>{t("modelProviders.bedrockEndpoint", { region: p.model_region ?? "—" })}</span>
          ) : (
            <code title={p.base_url ?? ""}>{endpointHost(p.base_url)}</code>
          )}
        </div>
        <div>
          <span className="k">{t("modelProviders.field.credential")}</span>
          {cred === "host_env" ? (
            <span className="v2-mp-cred env" data-testid={`v2-mp-cred-${p.name}`}>
              {t("modelProviders.credential.hostEnv", { env: p.credential_env ?? "" })}
            </span>
          ) : cred === "configured" ? (
            <span className="v2-mp-cred ok" data-testid={`v2-mp-cred-${p.name}`}>
              {keyLast4(p) ? t("modelProviders.credential.configuredLast4", { last4: keyLast4(p) }) : t("modelProviders.credential.configured")}
              {p.key_version != null && ` · ${t("modelProviders.credential.keyVersion", { version: p.key_version })}`}
              {p.key_modified_at && ` · ${t("modelProviders.credential.modified", { time: fmtTime(p.key_modified_at) })}`}
            </span>
          ) : (
            <span className="v2-mp-cred missing" data-testid={`v2-mp-cred-${p.name}`}>
              {t("modelProviders.credential.missing", { parameter: p.credential_ssm_parameter ?? "" })}
            </span>
          )}
        </div>
        <div>
          <span className="k">{t("modelProviders.field.identityProvider")}</span>
          <code>{p.credential_provider}</code>
        </div>
        <div>
          <span className="k">{t("modelProviders.field.lastProbe")}</span>
          {probe?.state === "ok" ? (
            <span>
              {t("modelProviders.probe.ok", { latency: probe.latency_ms ?? 0 })} · {fmtTime(probe.checked_at)}
              {probe.models_seen.length > 0 && ` · ${t("modelProviders.probe.modelsSeen", { count: probe.models_seen.length })}`}
            </span>
          ) : probe?.state === "failed" ? (
            <span className="v2-mp-cred missing" title={probe.error ?? ""}>
              {t("modelProviders.probe.failed")} · {fmtTime(probe.checked_at)}
            </span>
          ) : (
            <span className="v2-muted">{t("modelProviders.probe.never")}</span>
          )}
        </div>
        <div>
          <span className="k">{t("modelProviders.field.models")}</span>
          <span>{t("modelProviders.count.models", { count: modelCount })}</span>
        </div>
      </div>
      <div className="foot" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
        {mutable ? (
          <>
            <LinkButton disabled={busy} onClick={onProbe} testId={`v2-mp-probe-${p.name}`}>
              {t("modelProviders.action.probe")}
            </LinkButton>
            {cred !== "host_env" && (
              <>
                {/* N17-obs203 §3: type (or replace) the key here — written to SSM by the console host */}
                <LinkButton disabled={busy} onClick={() => actions.askReplaceKey(p)} testId={`v2-mp-replace-key-${p.name}`}>
                  {t(keyLast4(p) ? "modelProviders.action.replaceKey" : "modelProviders.action.enterKey")}
                </LinkButton>
                <LinkButton disabled={busy} onClick={() => void actions.credential(p)} testId={`v2-mp-credential-${p.name}`}>
                  {t(cred === "configured" ? "modelProviders.action.rotate" : "modelProviders.action.registerCredential")}
                </LinkButton>
              </>
            )}
            <LinkButton disabled={busy} onClick={onEdit} testId={`v2-mp-edit-${p.name}`}>
              {t("v2.common.edit")}
            </LinkButton>
            {!p.builtin && (
              <LinkButton danger disabled={busy} onClick={() => actions.askDeleteProvider(p)} testId={`v2-mp-delete-${p.name}`}>
                {t("v2.common.delete")}
              </LinkButton>
            )}
          </>
        ) : (
          // N17-obs203: another creator's (or the platform's) row — read-only for this member
          <span className="v2-muted" data-testid={`v2-mp-readonly-${p.name}`}>
            {t("v2.common.creatorOnly")}
          </span>
        )}
      </div>
    </div>
  );
}
