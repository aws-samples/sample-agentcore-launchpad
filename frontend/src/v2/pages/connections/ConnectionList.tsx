import { Copy, Plus, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import {
  api,
  type ConnectionInfo,
  type ConnectionKind,
  type ConnectionTemplate,
  type ConnectionTemplateField,
  errorMessage,
} from "../../../lib/api";
import { splitScopes } from "../../../lib/agent-spec";
import { EMPTY_OBO_FORM, oboAvailability, oboFromForm, type OboFormState } from "../../../lib/obo";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, type Column, Confirm, Field, LinkButton, Modal, Segmented, Table, Tag } from "../../ui";

const NAME_RE = /^[a-zA-Z0-9\-_]{1,128}$/;

export function ConnectionList() {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useV2Toast();
  const manage = can("identity.manage");
  const { data, loading, error, reload } = useLoad(() => api.listConnections(), "connections");
  const [creating, setCreating] = useState(false);
  const [callback, setCallback] = useState<ConnectionInfo | null>(null);
  const [removing, setRemoving] = useState<ConnectionInfo | null>(null);
  const [busy, setBusy] = useState(false);

  const showCallback = async (row: ConnectionInfo) => {
    try {
      // re-read: the list's value may be the create-time snapshot
      setCallback(await api.getConnection(row.kind, row.name));
    } catch (err) {
      toast("error", errorMessage(err));
    }
  };
  const remove = async () => {
    if (!removing) return;
    setBusy(true);
    try {
      await api.deleteConnection(removing.kind, removing.name);
      toast("success", t("v2.connections.deleted", { name: removing.name }));
      setRemoving(null);
      reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<ConnectionInfo>[] = [
    {
      key: "name",
      title: t("v2.connections.colName"),
      render: (row) => (
        <span className="v2-conn-name">
          <span className="mono">{row.name}</span>
          {row.description && <span className="v2-muted">{row.description}</span>}
        </span>
      ),
    },
    { key: "kind", title: t("v2.connections.colKind"), render: (row) => <KindTag kind={row.kind} /> },
    {
      key: "vendor",
      title: t("v2.connections.colVendor"),
      render: (row) => (row.template ? t(`v2.connections.template.${row.template}`, row.vendor) : row.vendor || "—"),
    },
    { key: "source", title: t("v2.connections.colSource"), render: (row) => <SourceTag row={row} /> },
    {
      key: "refs",
      title: t("v2.connections.colRefs"),
      render: (row) =>
        row.referenced_by.length === 0 ? (
          <span className="v2-muted">—</span>
        ) : (
          <span title={row.referenced_by.map((r) => `${t(`v2.connections.refType.${r.type}`)}: ${r.name}`).join("\n")}>
            {t("v2.connections.refCount", { count: row.referenced_by.length })}
          </span>
        ),
    },
    { key: "created", title: t("v2.connections.colCreated"), render: (row) => (row.created_at ? fmtTime(row.created_at) : "—") },
    {
      key: "actions",
      title: "",
      render: (row) => (
        <span className="v2-conn-actions">
          {row.kind === "oauth2" && row.status === "ready" && (
            <LinkButton onClick={() => void showCallback(row)} testId={`v2-conn-callback-${row.name}`}>
              {t("v2.connections.callback")}
            </LinkButton>
          )}
          {/* only Connections Launchpad created: an external vault provider may back
              another team's runtime, and the backend refuses it (409) */}
          {manage && !row.system && row.source === "launchpad" && (
            <LinkButton danger onClick={() => setRemoving(row)} testId={`v2-conn-delete-${row.name}`}>
              {t("v2.common.delete")}
            </LinkButton>
          )}
        </span>
      ),
    },
  ];

  return (
    <Card
      title={t("v2.connections.listTitle")}
      sub={t("v2.connections.listSub")}
      flush
      testId="v2-connections"
      end={
        <>
          <Button onClick={reload} title={t("v2.common.refresh")}>
            <RefreshCw size={14} aria-hidden="true" />
          </Button>
          {manage && (
            <Button kind="primary" onClick={() => setCreating(true)} testId="v2-conn-new">
              <Plus size={14} aria-hidden="true" /> {t("v2.connections.new")}
            </Button>
          )}
        </>
      }
    >
      <Table
        columns={columns}
        rows={data?.connections ?? []}
        rowKey={(row) => `${row.kind}:${row.name}`}
        loading={loading}
        error={error}
        onRetry={reload}
        empty={t("v2.connections.empty")}
      />
      {creating && (
        <CreateConnection
          onClose={() => setCreating(false)}
          onCreated={(created) => {
            setCreating(false);
            reload();
            if (created.kind === "oauth2") setCallback(created);
            else toast("success", t("v2.connections.created", { name: created.name }));
          }}
        />
      )}
      <CallbackDialog connection={callback} onClose={() => setCallback(null)} />
      <Confirm
        open={removing !== null}
        title={t("v2.connections.deleteTitle", { name: removing?.name ?? "" })}
        body={
          removing?.referenced_by.length ? (
            <Alert tone="warn">
              {t("v2.connections.deleteReferenced", {
                names: removing.referenced_by.map((r) => r.name).join(", "),
              })}
            </Alert>
          ) : (
            t("v2.connections.deleteBody")
          )
        }
        confirmLabel={t("v2.common.delete")}
        danger
        busy={busy}
        onConfirm={() => void remove()}
        onClose={() => setRemoving(null)}
      />
    </Card>
  );
}

export function KindTag({ kind }: { kind: ConnectionKind | string }) {
  const { t } = useTranslation();
  return <Tag tone={kind === "api_key" ? "orange" : "blue"}>{t(`identity.kind.${kind}`, kind)}</Tag>;
}

function SourceTag({ row }: { row: ConnectionInfo }) {
  const { t } = useTranslation();
  if (row.status === "missing")
    return (
      <Tag tone="red" dot title={t("v2.connections.missingHint")}>
        {t("v2.connections.status.missing")}
      </Tag>
    );
  const tone = row.source === "system" ? "gray" : row.source === "external" ? "outline" : "green";
  return (
    <Tag tone={tone} dot>
      {t(`v2.connections.source.${row.source}`)}
    </Tag>
  );
}

/* ── create ─────────────────────────────────────────────────────────────── */

type Values = Partial<Record<ConnectionTemplateField | "name" | "description", string>>;

function CreateConnection({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (created: ConnectionInfo) => void;
}) {
  const { t } = useTranslation();
  const templates = useLoad(() => api.connectionTemplates(), "connection-templates");
  const list = useMemo(() => templates.data?.templates ?? [], [templates.data]);
  const [templateId, setTemplateId] = useState("cognito");
  const template: ConnectionTemplate | undefined = list.find((x) => x.id === templateId) ?? list[0];
  const [values, setValues] = useState<Values>({});
  const [obo, setObo] = useState<OboFormState>({ ...EMPTY_OBO_FORM });
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const val = (key: keyof Values) => values[key] ?? "";
  const put = (key: keyof Values, value: string) => setValues((prev) => ({ ...prev, [key]: value }));

  const fields = template?.fields ?? [];
  const oboOffer = oboAvailability(template);
  const required: ConnectionTemplateField[] = fields.filter((f) => f !== "scopes");
  const problems: Partial<Record<keyof Values, string>> = {};
  if (!NAME_RE.test(val("name"))) problems.name = t("v2.connections.errName");
  for (const field of required) if (!val(field).trim()) problems[field] = t("v2.connections.errRequired");
  if (fields.includes("discovery_url") && val("discovery_url") &&
      !/^https:\/\/.+\/\.well-known\/(openid-configuration|oauth-authorization-server)$/.test(val("discovery_url").trim()))
    problems.discovery_url = t("v2.connections.errDiscovery");
  const err = (key: keyof Values) => (touched ? problems[key] : undefined);

  const submit = async () => {
    setTouched(true);
    if (!template || Object.keys(problems).length) return;
    setBusy(true);
    setError(null);
    const opt = (key: keyof Values) => val(key).trim() || undefined;
    try {
      const created =
        template.kind === "api_key"
          ? await api.createApiKeyConnection({ name: val("name"), description: opt("description"), api_key: val("api_key") })
          : await api.createOauth2Connection({
              name: val("name"),
              vendor: template.vendor,
              template: template.id,
              description: opt("description"),
              client_id: val("client_id").trim(),
              client_secret: val("client_secret"),
              discovery_url: opt("discovery_url"),
              issuer: opt("issuer"),
              authorization_endpoint: opt("authorization_endpoint"),
              token_endpoint: opt("token_endpoint"),
              scopes: splitScopes(val("scopes")),
              obo: oboOffer === "offered" ? oboFromForm(obo) : undefined,
            });
      onCreated(created);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const input = (field: ConnectionTemplateField, secret = false) => (
    <Field
      key={field}
      label={t(`v2.connections.field.${field}`)}
      required={field !== "scopes"}
      error={err(field)}
      hint={
        field === "discovery_url"
          ? template?.discovery_hint
          : field === "scopes"
            ? t("v2.connections.scopesHint")
            : secret
              ? t("v2.connections.secretHint")
              : undefined
      }
      full
    >
      <input
        className="v2-input mono"
        type={secret ? "password" : "text"}
        autoComplete={secret ? "new-password" : "off"}
        value={val(field)}
        onChange={(e) => put(field, e.target.value)}
        data-testid={`v2-conn-field-${field}`}
      />
    </Field>
  );

  return (
    <Modal
      open
      wide
      title={t("v2.connections.createTitle")}
      onClose={onClose}
      testId="v2-conn-create"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          <Button kind="primary" disabled={busy || !template} onClick={() => void submit()} testId="v2-conn-create-submit">
            {t("v2.connections.create")}
          </Button>
        </>
      }
    >
      <div className="v2-form">
        <Field label={t("v2.connections.field.template")} full>
          <div className="v2-conn-templates" role="radiogroup">
            {list.map((x) => (
              <button
                key={x.id}
                type="button"
                role="radio"
                aria-checked={x.id === template?.id}
                className={`v2-conn-template${x.id === template?.id ? " on" : ""}`}
                onClick={() => setTemplateId(x.id)}
                data-testid={`v2-conn-template-${x.id}`}
              >
                <strong>{t(`v2.connections.template.${x.id}`, x.vendor)}</strong>
                <KindTag kind={x.kind} />
              </button>
            ))}
          </div>
        </Field>
        <Field label={t("v2.connections.field.name")} required error={err("name")} hint={t("v2.connections.nameHint")} full>
          <input
            className="v2-input mono"
            value={val("name")}
            onChange={(e) => put("name", e.target.value)}
            data-testid="v2-conn-field-name"
          />
        </Field>
        <Field label={t("v2.connections.field.description")} full>
          <input className="v2-input" value={val("description")} maxLength={200} onChange={(e) => put("description", e.target.value)} />
        </Field>
        {fields.map((field) => input(field, field === "client_secret" || field === "api_key"))}
        {oboOffer === "cognito" && (
          <Alert tone="info">
            {t("v2.connections.obo.cognito")}
          </Alert>
        )}
        {oboOffer === "offered" && <OboFields form={obo} onChange={setObo} />}
        {error && <Alert tone="error">{error}</Alert>}
      </div>
    </Modal>
  );
}

/** On-behalf-of token exchange for a CustomOauth2 connection (gateway `obo` targets use it). */
function OboFields({ form, onChange }: { form: OboFormState; onChange: (next: OboFormState) => void }) {
  const { t } = useTranslation();
  const set = (patch: Partial<OboFormState>) => onChange({ ...form, ...patch });
  return (
    <div className="v2-form" data-testid="v2-conn-obo">
      <Field label={t("v2.connections.obo.title")} hint={t("v2.connections.obo.hint")} full>
        <label className="v2-check">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => set({ enabled: e.target.checked })}
            data-testid="v2-conn-obo-enabled"
          />
          {t("v2.connections.obo.enable")}
        </label>
      </Field>
      {form.enabled && (
        <>
          <Field label={t("v2.connections.obo.grant")} full>
            <Segmented<OboFormState["grant_type"]>
              value={form.grant_type}
              ariaLabel={t("v2.connections.obo.grant")}
              options={[
                { value: "TOKEN_EXCHANGE", label: t("v2.connections.obo.grantExchange") },
                { value: "JWT_AUTHORIZATION_GRANT", label: t("v2.connections.obo.grantJwtBearer") },
              ]}
              onChange={(grant_type) => set({ grant_type })}
            />
          </Field>
          {form.grant_type === "TOKEN_EXCHANGE" && (
            <Field label={t("v2.connections.obo.actor")} hint={t("v2.connections.obo.actorHint")} full>
              <Segmented<OboFormState["actor_token_content"]>
                value={form.actor_token_content}
                ariaLabel={t("v2.connections.obo.actor")}
                options={[
                  { value: "NONE", label: t("v2.connections.obo.actorNone") },
                  { value: "M2M", label: t("v2.connections.obo.actorM2m") },
                ]}
                onChange={(actor_token_content) => set({ actor_token_content })}
              />
            </Field>
          )}
          {form.grant_type === "TOKEN_EXCHANGE" && form.actor_token_content === "M2M" && (
            <Field label={t("v2.connections.obo.actorScopes")} hint={t("v2.connections.scopesHint")} full>
              <input
                className="v2-input mono"
                value={form.actor_token_scopes}
                onChange={(e) => set({ actor_token_scopes: e.target.value })}
                data-testid="v2-conn-obo-actor-scopes"
              />
            </Field>
          )}
        </>
      )}
    </div>
  );
}

/* ── callback URL ───────────────────────────────────────────────────────── */

function CallbackDialog({ connection, onClose }: { connection: ConnectionInfo | null; onClose: () => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const url = connection?.callback_url ?? "";
  return (
    <Modal
      open={connection !== null}
      title={t("v2.connections.callbackTitle", { name: connection?.name ?? "" })}
      onClose={onClose}
      testId="v2-conn-callback"
      footer={<Button kind="primary" onClick={onClose}>{t("v2.connections.callbackDone")}</Button>}
    >
      <div className="v2-form">
        <Alert tone="info">{t("v2.connections.callbackBody")}</Alert>
        {url ? (
          <div className="v2-conn-callback">
            <code className="mono">{url}</code>
            <Button
              size="sm"
              onClick={() =>
                void navigator.clipboard
                  ?.writeText(url)
                  .then(() => toast("success", t("v2.connections.copied")))
                  .catch(() => undefined)
              }
              title={t("v2.connections.copy")}
            >
              <Copy size={13} aria-hidden="true" />
            </Button>
          </div>
        ) : (
          <Alert tone="warn">{t("v2.connections.callbackMissing")}</Alert>
        )}
        <p className="v2-muted">{t("v2.connections.callbackM2m")}</p>
      </div>
    </Modal>
  );
}
