import { Plus, RefreshCw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { splitScopes } from "../../../lib/agent-spec";
import {
  type ActingMode,
  api,
  type ConnectionInfo,
  type CreateIdentityGatewayTargetInput,
  errorMessage,
  type IdentityGatewayTarget,
  type TargetWarning,
} from "../../../lib/api";
import { targetModes } from "../../../lib/obo";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, type Column, Confirm, Field, LinkButton, Modal, Segmented, Select, Table, Tag } from "../../ui";
import { KindTag } from "./ConnectionList";

const TARGET_NAME_RE = /^([0-9a-zA-Z][-]?){1,100}$/;
const STATUS_TONE: Record<string, "green" | "orange" | "red"> = {
  READY: "green",
  CREATING: "orange",
  UPDATING: "orange",
  FAILED: "red",
};

/** Gateway targets on the workspace gateway, and which Connection each calls through. */
export function GatewayTargets() {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useV2Toast();
  const navigate = useNavigate();
  const manage = can("identity.manage");
  const { data, loading, error, reload } = useLoad(() => api.listIdentityGatewayTargets(), "identity-targets");
  // tool-level Cedar: the governance editor, prefilled with this target's exact actions
  const policyLink = (row: IdentityGatewayTarget) =>
    data?.gateway_id ? (
      <LinkButton
        onClick={() =>
          navigate(`/v2/governance?${new URLSearchParams({ view: "policy", gateway: data.gateway_id ?? "", target: row.name })}`)
        }
        testId={`v2-target-policy-${row.name}`}
      >
        {t("v2.connections.targets.policy")}
      </LinkButton>
    ) : null;
  const [creating, setCreating] = useState(false);
  const [removing, setRemoving] = useState<IdentityGatewayTarget | null>(null);
  const [busy, setBusy] = useState(false);
  // the last create's non-blocking hints; kept on the card until dismissed
  const [warnings, setWarnings] = useState<TargetWarning[]>([]);

  const remove = async () => {
    if (!removing) return;
    setBusy(true);
    try {
      await api.deleteIdentityGatewayTarget(removing.target_id);
      toast("success", t("v2.connections.targets.deleted", { name: removing.name }));
      setRemoving(null);
      reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<IdentityGatewayTarget>[] = [
    { key: "name", title: t("v2.connections.targets.colName"), render: (row) => <span className="mono">{row.name}</span> },
    {
      key: "source",
      title: t("v2.connections.targets.colSource"),
      render: (row) => t(`v2.connections.targets.source.${row.source}`, row.source),
    },
    {
      key: "connection",
      title: t("v2.connections.targets.colConnection"),
      render: (row) =>
        row.connection ? (
          <span className="v2-conn-name">
            <span className="mono">{row.connection}</span>
            <KindTag kind={row.auth} />
          </span>
        ) : (
          <span className="v2-muted">{t(`v2.connections.targets.auth.${row.auth}`, row.auth)}</span>
        ),
    },
    {
      key: "mode",
      title: t("v2.connections.targets.colMode"),
      render: (row) => (row.mode ? <Tag tone="blue">{t(`identity.mode.${row.mode}`, row.mode)}</Tag> : "—"),
    },
    {
      key: "status",
      title: t("v2.connections.targets.colStatus"),
      render: (row) => (
        <Tag tone={STATUS_TONE[row.status] ?? "gray"} dot title={row.status_reasons.join("\n") || undefined}>
          {row.status || "—"}
        </Tag>
      ),
    },
    {
      key: "actions",
      title: "",
      render: (row) => (
        <span className="v2-row">
          {policyLink(row)}
          {row.system ? (
            <Tag tone="gray">{t("v2.connections.source.system")}</Tag>
          ) : manage && row.connection ? (
            <LinkButton danger onClick={() => setRemoving(row)} testId={`v2-target-delete-${row.name}`}>
              {t("v2.common.delete")}
            </LinkButton>
          ) : null}
        </span>
      ),
    },
  ];

  const noGateway = data !== null && !data.gateway_id;
  return (
    <Card
      title={t("v2.connections.targets.title")}
      sub={data?.gateway_id ? t("v2.connections.targets.sub", { id: data.gateway_id }) : t("v2.connections.targets.subNone")}
      flush
      testId="v2-identity-targets"
      end={
        <>
          <Button onClick={reload} title={t("v2.common.refresh")}>
            <RefreshCw size={14} aria-hidden="true" />
          </Button>
          {manage && (
            <Button kind="primary" disabled={noGateway} onClick={() => setCreating(true)} testId="v2-target-new">
              <Plus size={14} aria-hidden="true" /> {t("v2.connections.targets.new")}
            </Button>
          )}
        </>
      }
    >
      {noGateway && <Alert tone="warn">{t("v2.connections.targets.noGateway")}</Alert>}
      {warnings.map((warning) => (
        <Alert
          key={warning.code}
          tone="warn"
          action={<LinkButton onClick={() => setWarnings([])}>{t("v2.common.close")}</LinkButton>}
        >
          <span data-testid="v2-target-warning">{targetWarningText(t, warning)}</span>
        </Alert>
      ))}
      <Table
        columns={columns}
        rows={data?.targets ?? []}
        rowKey={(row) => row.target_id}
        loading={loading}
        error={error}
        onRetry={reload}
        empty={t("v2.connections.targets.empty")}
      />
      {creating && (
        <CreateTarget
          onClose={() => setCreating(false)}
          onCreated={(created) => {
            setCreating(false);
            setWarnings(created.warnings ?? []);
            toast("success", t("v2.connections.targets.created", { name: created.name }));
            reload();
          }}
        />
      )}
      <Confirm
        open={removing !== null}
        title={t("v2.connections.targets.deleteTitle", { name: removing?.name ?? "" })}
        body={t("v2.connections.targets.deleteBody")}
        confirmLabel={t("v2.common.delete")}
        danger
        busy={busy}
        onConfirm={() => void remove()}
        onClose={() => setRemoving(null)}
      />
    </Card>
  );
}

/** Console copy for a create warning; an unknown code keeps the backend's English. */
function targetWarningText(t: (key: string, options?: Record<string, unknown>) => string, warning: TargetWarning): string {
  if (warning.code !== "identity.obo_issuer_mismatch") return warning.message;
  return t("v2.connections.targets.oboIssuerMismatch", {
    connection: warning.detail.connection ?? "",
    connectionIssuer: warning.detail.connection_issuer ?? "",
    gatewayIssuer: warning.detail.gateway_issuer ?? "",
  });
}

function CreateTarget({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (created: IdentityGatewayTarget) => void;
}) {
  const { t } = useTranslation();
  const conns = useLoad(() => api.listConnections(), "connections");
  const [source, setSource] = useState<"openapi" | "mcp">("openapi");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [schema, setSchema] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [connection, setConnection] = useState("");
  const [scopes, setScopes] = useState("");
  const [location, setLocation] = useState<"HEADER" | "QUERY_PARAMETER">("HEADER");
  const [parameter, setParameter] = useState("Authorization");
  const [prefix, setPrefix] = useState("");
  const [mode, setMode] = useState<ActingMode>("as_agent");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // an MCP server target takes OAuth only (service model + devguide, docs/identity.md §1.4)
  const usable = (conns.data?.connections ?? []).filter(
    (c: ConnectionInfo) => c.status === "ready" && (source === "openapi" || c.kind === "oauth2"),
  );
  const picked = usable.find((c) => `${c.kind}:${c.name}` === connection) ?? null;
  const modes = targetModes(picked?.kind);
  const actingMode: ActingMode = modes.includes(mode) ? mode : "as_agent";

  const problems: Record<string, string> = {};
  if (!TARGET_NAME_RE.test(name)) problems.name = t("v2.connections.targets.errName");
  if (source === "openapi" && !schema.trim()) problems.schema = t("v2.connections.errRequired");
  if (source === "mcp" && !/^https:\/\//.test(endpoint.trim())) problems.endpoint = t("v2.connections.targets.errEndpoint");
  if (!picked) problems.connection = t("v2.connections.targets.errConnection");
  const err = (key: string) => (touched ? problems[key] : undefined);

  const submit = async () => {
    setTouched(true);
    if (Object.keys(problems).length || !picked) return;
    setBusy(true);
    setError(null);
    const input: CreateIdentityGatewayTargetInput = {
      name,
      description: description.trim() || undefined,
      source,
      ...(source === "openapi" ? { openapi_schema: schema } : { mcp_endpoint: endpoint.trim() }),
      connection: picked.name,
      kind: picked.kind,
      mode: actingMode,
      scopes: picked.kind === "oauth2" ? splitScopes(scopes) : [],
      ...(picked.kind === "api_key"
        ? { api_key: { location, parameter_name: parameter.trim() || "Authorization", prefix: prefix.trim() || undefined } }
        : {}),
    };
    try {
      onCreated(await api.createIdentityGatewayTarget(input));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      wide
      title={t("v2.connections.targets.createTitle")}
      onClose={onClose}
      testId="v2-target-create"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          <Button kind="primary" disabled={busy} onClick={() => void submit()} testId="v2-target-create-submit">
            {t("v2.connections.create")}
          </Button>
        </>
      }
    >
      <div className="v2-form">
        <Field label={t("v2.connections.targets.colSource")} full>
          <Segmented
            value={source}
            onChange={(next) => {
              setSource(next);
              setConnection("");
            }}
            options={(["openapi", "mcp"] as const).map((value) => ({ value, label: t(`v2.connections.targets.source.${value}`) }))}
          />
        </Field>
        <Field label={t("v2.connections.targets.colName")} required error={err("name")} hint={t("v2.connections.targets.nameHint")} full>
          <input className="v2-input mono" value={name} onChange={(e) => setName(e.target.value)} data-testid="v2-target-name" />
        </Field>
        <Field label={t("v2.connections.field.description")} full>
          <input className="v2-input" value={description} maxLength={200} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        {source === "openapi" ? (
          <Field label={t("v2.connections.targets.schema")} required error={err("schema")} hint={t("v2.connections.targets.schemaHint")} full>
            <textarea
              className="v2-textarea code"
              rows={8}
              value={schema}
              onChange={(e) => setSchema(e.target.value)}
              data-testid="v2-target-schema"
            />
          </Field>
        ) : (
          <Field label={t("v2.connections.targets.endpoint")} required error={err("endpoint")} full>
            <input
              className="v2-input mono"
              value={endpoint}
              placeholder="https://mcp.example.com/mcp"
              onChange={(e) => setEndpoint(e.target.value)}
              data-testid="v2-target-endpoint"
            />
          </Field>
        )}
        <Field
          label={t("v2.connections.targets.colConnection")}
          required
          error={err("connection")}
          hint={source === "mcp" ? t("v2.connections.targets.mcpOauthOnly") : undefined}
          full
        >
          <Select
            value={connection}
            onChange={setConnection}
            ariaLabel={t("v2.connections.targets.colConnection")}
            testId="v2-target-connection"
            placeholder={t("v2.common.choose")}
            options={usable.map((c) => ({ value: `${c.kind}:${c.name}`, label: `${c.name} · ${t(`identity.kind.${c.kind}`)}` }))}
          />
        </Field>
        <Field label={t("v2.connections.targets.colMode")} hint={t(`v2.connections.targets.modeHints.${actingMode}`)} full>
          {modes.length > 1 ? (
            <div data-testid="v2-target-mode">
              <Segmented<ActingMode>
                value={actingMode}
                ariaLabel={t("v2.connections.targets.colMode")}
                options={modes.map((value) => ({ value, label: t(`identity.mode.${value}`) }))}
                onChange={setMode}
              />
            </div>
          ) : (
            <span>
              <Tag tone="blue">{t("identity.mode.as_agent")}</Tag>
            </span>
          )}
        </Field>
        {actingMode === "obo" && <Alert tone="info">{t("v2.connections.targets.oboNeeds")}</Alert>}
        {picked?.kind === "oauth2" && (
          <Field label={t("v2.connections.field.scopes")} hint={t("v2.connections.scopesHint")} full>
            <input className="v2-input mono" value={scopes} onChange={(e) => setScopes(e.target.value)} data-testid="v2-target-scopes" />
          </Field>
        )}
        {picked?.kind === "api_key" && (
          <div className="v2-grid-3">
            <Field label={t("identity.tools.keyIn")}>
              <Select
                value={location}
                onChange={(v) => setLocation(v as typeof location)}
                ariaLabel={t("identity.tools.keyIn")}
                testId="v2-target-key-location"
                options={[
                  { value: "HEADER", label: t("identity.tools.keyInHeader") },
                  { value: "QUERY_PARAMETER", label: t("identity.tools.keyInQuery") },
                ]}
              />
            </Field>
            <Field label={t("identity.tools.keyName")}>
              <input className="v2-input mono" value={parameter} onChange={(e) => setParameter(e.target.value)} />
            </Field>
            <Field label={t("v2.connections.targets.prefix")}>
              <input className="v2-input mono" value={prefix} placeholder="Bearer" onChange={(e) => setPrefix(e.target.value)} />
            </Field>
          </div>
        )}
        {error && <Alert tone="error">{error}</Alert>}
      </div>
    </Modal>
  );
}
