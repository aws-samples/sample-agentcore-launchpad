import { Plus, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { api, errorMessage, type InboundAuth, type InboundCustomClaim, type JwtInboundConfig } from "../../../lib/api";
import {
  applyOidcSource,
  EMPTY_CLAIM,
  EMPTY_JWT_FORM,
  inboundCapable,
  type InboundChoice,
  issuerMismatch,
  type JwtClaimForm,
  jwtConfigFromForm,
  type JwtFormState,
  jwtFormFromConfig,
  jwtFormProblem,
  jwtSummary,
  m2mCurlExample,
  REACHABILITY_KEYS,
  type ReachabilityScope,
  switchDialogInitial,
  withDiscoveryUrl,
} from "../../../lib/inbound-auth";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, Descriptions, Field, LinkButton, Modal, Segmented, Select, Tag } from "../../ui";
import type { SectionProps } from "./wizardKit";

const CLAIM_VALUE_TYPES: InboundCustomClaim["value_type"][] = ["STRING", "STRING_ARRAY"];
const CLAIM_OPERATORS: InboundCustomClaim["match_operator"][] = ["EQUALS", "CONTAINS", "CONTAINS_ANY"];

/**
 * The JWT-authorizer field set (discovery URL, allowed lists, custom-claim rows).
 * Mode selection stays with the caller: the workspace editor offers iam/jwt, the
 * wizard inherit/iam/jwt, the agent page's switch dialog JWT only.
 *
 * The discovery URL can be typed, taken from the workspace Cognito preset, or
 * picked from an OAuth2 Connection (its derivable OIDC discovery URL only —
 * never its client id). A URL whose issuer is not the workspace pool's gets the
 * console-reachability warning.
 */
export function JwtConfigFields({
  form,
  onChange,
  showProblem,
  idPrefix = "v2-ia",
  cognito = null,
  cognitoIssuer = null,
  workspaceId = null,
  scope = "agent",
}: {
  form: JwtFormState;
  onChange: (next: JwtFormState) => void;
  showProblem: boolean;
  idPrefix?: string;
  /** the workspace Cognito preset (null before bootstrap: no preset button) */
  cognito?: JwtInboundConfig | null;
  /** the workspace pool's issuer — what platform invokes present */
  cognitoIssuer?: string | null;
  /** the workspace whose Connections the picker lists (default: the active one) */
  workspaceId?: string | null;
  /** what the config applies to: one agent, or the workspace default its inheritors resolve to */
  scope?: ReachabilityScope;
}) {
  const { t } = useTranslation();
  const sources = useLoad(() => api.listOidcSources(workspaceId), `oidc-sources:${workspaceId ?? ""}`);
  const set = (patch: Partial<JwtFormState>) => onChange({ ...form, ...patch });
  const setClaim = (index: number, patch: Partial<JwtClaimForm>) =>
    set({ custom_claims: form.custom_claims.map((c, i) => (i === index ? { ...c, ...patch } : c)) });
  const problem = jwtFormProblem(form);
  const mismatch = issuerMismatch(form.discovery_url, cognitoIssuer);
  const options = sources.data?.sources ?? [];
  const pickerLabel = sources.error
    ? t("inboundAuth.idpSource.unavailable")
    : !sources.loading && !options.length
      ? t("inboundAuth.idpSource.none")
      : t("inboundAuth.idpSource.pickConnection");
  const list = (key: "allowed_clients" | "allowed_audience" | "allowed_scopes", label: string) => (
    <Field label={t(label)}>
      <input
        className="v2-input mono"
        value={form[key]}
        onChange={(e) => set({ [key]: e.target.value })}
        data-testid={`${idPrefix}-${key}`}
      />
    </Field>
  );
  return (
    <div className="v2-form" data-testid={`${idPrefix}-jwt-fields`}>
      <Field label={t("inboundAuth.idpSource.label")} hint={t("inboundAuth.idpSource.clientHint")} full>
        <div className="v2-row" data-testid={`${idPrefix}-idp-source`}>
          {cognito && (
            <Button size="sm" onClick={() => onChange(jwtFormFromConfig(cognito))} testId={`${idPrefix}-use-cognito`}>
              {t("inboundAuth.useCognito")}
            </Button>
          )}
          <Select
            value={options.some((o) => o.name === form.source_connection) ? form.source_connection : ""}
            disabled={!options.length}
            ariaLabel={t("inboundAuth.idpSource.pickConnection")}
            onChange={(v) => {
              const picked = options.find((o) => o.name === v);
              if (picked) onChange(applyOidcSource(form, picked));
            }}
            testId={`${idPrefix}-connection-source`}
            placeholder={pickerLabel}
            options={options.map((o) => ({ value: o.name, label: `${o.name} · ${o.issuer}` }))}
            style={{ width: "auto", minWidth: 220 }}
          />
          {form.source_connection && (
            <Tag tone="blue">{t("inboundAuth.idpSource.fromConnection", { name: form.source_connection })}</Tag>
          )}
        </div>
      </Field>
      <Field
        label={t("inboundAuth.discoveryUrl")}
        required
        full
        error={showProblem && problem === "inboundAuth.problems.discoveryUrl" ? t(problem) : null}
      >
        <input
          className="v2-input mono"
          value={form.discovery_url}
          placeholder="https://…/.well-known/openid-configuration"
          onChange={(e) => onChange(withDiscoveryUrl(form, e.target.value))}
          data-testid={`${idPrefix}-discovery-url`}
        />
      </Field>
      {mismatch && <ReachabilityWarning mismatch={mismatch} scope={scope} testId={`${idPrefix}-issuer-warning`} />}
      <div className="v2-field full">
        <div className="v2-ia-lists" data-testid={`${idPrefix}-lists`}>
          {list("allowed_clients", "inboundAuth.allowedClients")}
          {list("allowed_audience", "inboundAuth.allowedAudience")}
          {list("allowed_scopes", "inboundAuth.allowedScopes")}
        </div>
        <span className="hint">{t("inboundAuth.listPlaceholder")}</span>
      </div>
      <Field label={t("inboundAuth.customClaims")} hint={t("inboundAuth.customClaimsNote")} full>
        {form.custom_claims.length > 0 && (
          <div className="v2-agents-rows">
            {form.custom_claims.map((claim, index) => (
              <div key={index} className="v2-agents-row" data-testid={`${idPrefix}-claim-row`}>
                <input
                  className="v2-input mono"
                  value={claim.name}
                  placeholder={t("inboundAuth.claimName")}
                  aria-label={t("inboundAuth.claimName")}
                  onChange={(e) => setClaim(index, { name: e.target.value })}
                />
                <Select
                  mono
                  value={claim.value_type}
                  ariaLabel={t("inboundAuth.claimValueType")}
                  onChange={(v) => setClaim(index, { value_type: v as InboundCustomClaim["value_type"] })}
                  testId={`${idPrefix}-claim-type-${index}`}
                  options={CLAIM_VALUE_TYPES.map((v) => ({ value: v, label: v }))}
                />
                <Select
                  mono
                  value={claim.match_operator}
                  ariaLabel={t("inboundAuth.claimOperator")}
                  onChange={(v) => setClaim(index, { match_operator: v as InboundCustomClaim["match_operator"] })}
                  testId={`${idPrefix}-claim-operator-${index}`}
                  options={CLAIM_OPERATORS.map((v) => ({ value: v, label: v }))}
                />
                <input
                  className="v2-input mono"
                  value={claim.match_values}
                  placeholder={t("inboundAuth.claimValues")}
                  aria-label={t("inboundAuth.claimValues")}
                  onChange={(e) => setClaim(index, { match_values: e.target.value })}
                />
                <Button
                  size="sm"
                  title={t("v2.common.delete")}
                  onClick={() => set({ custom_claims: form.custom_claims.filter((_, i) => i !== index) })}
                >
                  <Trash2 size={13} aria-hidden="true" />
                </Button>
              </div>
            ))}
          </div>
        )}
        <div className="v2-agents-actions">
          <Button
            size="sm"
            onClick={() =>
              set({
                custom_claims: [...form.custom_claims, { ...EMPTY_CLAIM }],
              })
            }
            testId={`${idPrefix}-add-claim`}
          >
            <Plus size={13} aria-hidden="true" /> {t("inboundAuth.addClaim")}
          </Button>
        </div>
      </Field>
      {showProblem && problem && problem !== "inboundAuth.problems.discoveryUrl" && (
        <Alert tone="error">
          <span data-testid={`${idPrefix}-problem`}>{t(problem)}</span>
        </Alert>
      )}
    </div>
  );
}

/**
 * The console-reachability warning: an issuer other than the workspace pool's
 * means every platform invoke (Chat, /v1, direct invoke, evaluation) is refused.
 */
export function ReachabilityWarning({
  mismatch,
  testId,
  scope = "agent",
}: {
  mismatch: NonNullable<ReturnType<typeof issuerMismatch>>;
  testId: string;
  scope?: ReachabilityScope;
}) {
  const { t } = useTranslation();
  const keys = REACHABILITY_KEYS[scope];
  return (
    <Alert tone="warn">
      <span data-testid={testId}>
        <strong>{t(keys.title)}</strong>{" "}
        {t(keys.body, { issuer: mismatch.agentIssuer, workspaceIssuer: mismatch.workspaceIssuer })}
      </span>
    </Alert>
  );
}

/** Mode tag for an inbound-auth value. */
export function InboundModeTag({ mode }: { mode: "iam" | "jwt" }) {
  const { t } = useTranslation();
  return <Tag tone={mode === "jwt" ? "orange" : "blue"}>{t(`inboundAuth.mode.${mode}`)}</Tag>;
}

/**
 * The wizard's 入站认证 card (HTTP Runtime methods only): inherit the workspace
 * default (resolved at deploy), pin IAM, or pin a JWT authorizer — with the
 * workspace Cognito pool and the workspace's OAuth2 Connections as presets.
 */
export function InboundCard({
  form,
  set,
  touched,
  workspaceDefault,
  cognito,
  cognitoIssuer = null,
}: Pick<SectionProps, "form" | "set"> & {
  touched: boolean;
  workspaceDefault: InboundAuth | null;
  cognito: JwtInboundConfig | null;
  cognitoIssuer?: string | null;
}) {
  const { t } = useTranslation();
  if (!inboundCapable(form.method, form.protocol)) return null;
  const choice = form.inbound ?? "inherit";
  const jwt = form.inboundJwt ?? EMPTY_JWT_FORM;
  const inherited = workspaceDefault?.mode ?? "iam";
  return (
    <Card
      title={t("inboundAuth.title")}
      sub={t("inboundAuth.wizardSub")}
      testId="v2-agent-inbound"
      end={
        <Link to="/v2/workspaces" className="v2-link">
          {t("inboundAuth.manageDefault")}
        </Link>
      }
    >
      <div className="v2-form">
        <Field label={t("inboundAuth.modeLabel")} full>
          <Segmented<InboundChoice>
            value={choice}
            ariaLabel={t("inboundAuth.modeLabel")}
            options={[
              { value: "inherit", label: t("inboundAuth.choice.inherit") },
              { value: "iam", label: t("inboundAuth.choice.iam") },
              { value: "jwt", label: t("inboundAuth.choice.jwt") },
            ]}
            onChange={(next) => set({ inbound: next })}
          />
        </Field>
      </div>
      {choice === "inherit" && (
        <p className="v2-muted" data-testid="v2-agent-inbound-inherited">
          {t("inboundAuth.inheritNote")} <InboundModeTag mode={inherited} />
          {inherited === "jwt" && workspaceDefault?.jwt ? ` ${jwtSummary(workspaceDefault.jwt)}` : ""}
        </p>
      )}
      {choice === "iam" && <p className="v2-muted">{t("inboundAuth.iamNote")}</p>}
      {choice === "jwt" && (
        <>
          <Alert>{t("inboundAuth.jwtNote")}</Alert>
          <JwtConfigFields
            form={jwt}
            onChange={(next) => set({ inboundJwt: next })}
            showProblem={touched}
            cognito={cognito}
            cognitoIssuer={cognitoIssuer}
          />
        </>
      )}
    </Card>
  );
}

/**
 * 入站认证 on an agent page: the live mode, the pin, the caller example, and the
 * in-place switch (`POST /api/agents/{id}/inbound-auth`, a new version on the
 * SAME runtime). Switching to JWT opens an editable dialog prefilled with the
 * workspace default when it is JWT, else the workspace Cognito preset; switching
 * to IAM is a plain confirm.
 */
export function InboundSwitchCard({
  agentId,
  mode,
  pinned,
  capable,
  canSwitch,
  busy,
  jwt,
  invokeUrl,
  onSwitched,
}: {
  agentId: string;
  mode: "iam" | "jwt";
  pinned: "iam" | "jwt" | null;
  capable: boolean;
  canSwitch: boolean;
  /** a deploy is in flight — the backend would 409 */
  busy: boolean;
  jwt: {
    discovery_url: string;
    allowed_clients?: string[];
    allowed_audience?: string[];
    allowed_scopes?: string[];
    custom_claims?: unknown[];
    source_connection?: string | null;
  } | null;
  invokeUrl: string | null;
  onSwitched: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [confirming, setConfirming] = useState(false);
  const [saving, setSaving] = useState(false);
  const defaults = useLoad(() => api.getInboundAuthDefault(), "inbound-default");
  const target: "iam" | "jwt" = mode === "jwt" ? "iam" : "jwt";
  // The deployed authorizer, not just the dialog: a foreign issuer refuses Chat and /v1.
  const mismatch = mode === "jwt" && jwt ? issuerMismatch(jwt.discovery_url, defaults.data?.cognito_issuer) : null;
  // The JWT dialog opens prefilled from the defaults: it needs them loaded.
  const defaultsFailed = target === "jwt" && !defaults.data && !defaults.loading && !!defaults.error;
  const switchBlocked = busy || (target === "jwt" && !defaults.data);

  const toIam = async () => {
    setSaving(true);
    try {
      await api.switchInboundAuth(agentId, { mode: "iam" });
      toast("success", t("inboundAuth.switch.started", { mode: t("inboundAuth.mode.iam") }));
      setConfirming(false);
      onSwitched();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card
      title={t("inboundAuth.title")}
      sub={t("inboundAuth.detailSub")}
      testId="v2-agent-inbound-card"
      end={
        capable && canSwitch ? (
          <Button
            kind={target === "jwt" ? "primary" : undefined}
            disabled={switchBlocked}
            title={defaultsFailed ? t("inboundAuth.switch.defaultsFailed") : undefined}
            onClick={() => setConfirming(true)}
            testId="v2-agent-inbound-switch"
          >
            {t(`inboundAuth.switch.to.${target}`)}
          </Button>
        ) : undefined
      }
    >
      <Descriptions
        items={[
          { label: t("inboundAuth.modeLabel"), value: <InboundModeTag mode={mode} /> },
          {
            label: t("inboundAuth.pinLabel"),
            value: pinned ? t(`inboundAuth.pin.${pinned}`) : t("inboundAuth.pin.inherit"),
          },
          ...(mode === "jwt" && jwt
            ? [
                {
                  label: t("inboundAuth.discoveryUrl"),
                  value: (
                    <span className="v2-row">
                      <span className="mono">{jwt.discovery_url}</span>
                      {jwt.source_connection && (
                        <Tag tone="blue">
                          <span data-testid="v2-agent-inbound-source-connection">
                            {t("inboundAuth.idpSource.fromConnection", { name: jwt.source_connection })}
                          </span>
                        </Tag>
                      )}
                    </span>
                  ),
                },
                { label: t("inboundAuth.restrictions"), value: <span className="mono">{jwtSummary(jwt) || "—"}</span> },
              ]
            : []),
        ]}
      />
      {mismatch && <ReachabilityWarning mismatch={mismatch} testId="v2-agent-inbound-issuer-warning" />}
      {capable && canSwitch && defaultsFailed && (
        <Alert tone="error" action={<LinkButton onClick={defaults.reload}>{t("v2.common.retry")}</LinkButton>}>
          <span data-testid="v2-agent-inbound-defaults-error">
            {t("inboundAuth.switch.defaultsFailed")} {defaults.error}
          </span>
        </Alert>
      )}
      {!capable && <p className="v2-muted">{t("inboundAuth.notCapable")}</p>}
      {mode === "jwt" && invokeUrl && jwt && (
        <>
          <h3 className="v2-sub-title">{t("inboundAuth.callerTitle")}</h3>
          <p className="v2-muted">{t("inboundAuth.callerNote")}</p>
          <pre className="v2-pre" data-testid="v2-agent-inbound-curl">
            {m2mCurlExample(invokeUrl, jwt.discovery_url, jwt.allowed_scopes ?? [])}
          </pre>
        </>
      )}
      {target === "iam" && (
        <Confirm
          open={confirming}
          title={t("inboundAuth.switch.confirmTitle.iam")}
          confirmLabel={t("inboundAuth.switch.to.iam")}
          busy={saving}
          danger
          onClose={() => setConfirming(false)}
          onConfirm={() => void toIam()}
          body={
            <div data-testid="v2-agent-inbound-confirm">
              <p>{t("inboundAuth.switch.inPlace")}</p>
              <p>{t("inboundAuth.switch.effect.iam")}</p>
            </div>
          }
        />
      )}
      {target === "jwt" && confirming && defaults.data && (
        <SwitchToJwtDialog
          agentId={agentId}
          workspaceDefault={defaults.data.default}
          cognito={defaults.data.cognito}
          cognitoIssuer={defaults.data.cognito_issuer ?? null}
          onClose={() => setConfirming(false)}
          onSwitched={() => {
            setConfirming(false);
            onSwitched();
          }}
        />
      )}
    </Card>
  );
}

/**
 * The switch-to-JWT dialog: the full, editable JWT field set, prefilled from the
 * workspace default (else the Cognito preset). The same validation as the
 * wizard gates the submit; the backend's discovery probe answers by name.
 */
function SwitchToJwtDialog({
  agentId,
  workspaceDefault,
  cognito,
  cognitoIssuer,
  onClose,
  onSwitched,
}: {
  agentId: string;
  workspaceDefault: InboundAuth;
  cognito: JwtInboundConfig | null;
  cognitoIssuer: string | null;
  onClose: () => void;
  onSwitched: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [initial] = useState(() => switchDialogInitial(workspaceDefault, cognito));
  const [form, setForm] = useState<JwtFormState>(initial.form);
  const [touched, setTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Bumped on every refused submit: the inline error, the problem alert and the
  // probe error all render in the scroll body, usually below the fold at the footer.
  const [refused, setRefused] = useState(0);
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!refused) return;
    bodyRef.current?.querySelector(".err, [role=alert]")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [refused]);

  const submit = async () => {
    setTouched(true);
    if (jwtFormProblem(form)) {
      setRefused((n) => n + 1);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await api.switchInboundAuth(agentId, { mode: "jwt", jwt: jwtConfigFromForm(form) });
      toast("success", t("inboundAuth.switch.started", { mode: t("inboundAuth.mode.jwt") }));
      onSwitched();
    } catch (err) {
      setError(errorMessage(err));
      setRefused((n) => n + 1);
    } finally {
      setSaving(false);
    }
  };

  const prefillKey = { default: "fromDefault", cognito: "fromCognito", empty: "fromEmpty" }[initial.prefill];
  return (
    <Modal
      open
      wide
      tall
      title={t("inboundAuth.switch.confirmTitle.jwt")}
      onClose={onClose}
      testId="v2-agent-inbound-confirm"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          <Button kind="primary" disabled={saving} onClick={() => void submit()} testId="v2-agent-inbound-submit">
            {t("inboundAuth.switch.to.jwt")}
          </Button>
        </>
      }
    >
      <div ref={bodyRef}>
        <p>{t("inboundAuth.switch.inPlace")}</p>
        <Alert tone="warn">{t("inboundAuth.switch.effect.jwt")}</Alert>
        <p className="v2-muted" data-testid="v2-agent-inbound-prefill">
          {t("inboundAuth.switch.prefillNote", { source: t(`inboundAuth.switch.${prefillKey}`) })}
        </p>
        <JwtConfigFields
          form={form}
          onChange={setForm}
          showProblem={touched}
          idPrefix="v2-agent-ia"
          cognito={cognito}
          cognitoIssuer={cognitoIssuer}
        />
        {error && (
          <Alert tone="error">
            <span data-testid="v2-agent-inbound-error">{error}</span>
          </Alert>
        )}
      </div>
    </Modal>
  );
}
