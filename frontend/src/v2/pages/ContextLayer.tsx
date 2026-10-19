import { ExternalLink } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import {
  api,
  ApiError,
  type ContextLayerConnectInput,
  type ContextLayerNamespace,
  type ContextLayerStatus,
  errorMessage,
} from "../../lib/api";
import { fmtTime } from "../format";
import { useLoad, useV2Toast } from "../hooks";
import { Alert, Button, Card, Confirm, Descriptions, Field, Lamp, PageHeader, Spin, Table, Tag } from "../ui";
import { AuthorizationControl } from "./contextLayer/AuthorizationControl";
import { authStatusOf, healthRows, type HealthRow, namespaceLabel } from "./contextLayer/state";

const PREFIX_RE = /^[a-z][a-z0-9-]{1,30}$/;
const ENV_RE = /^[a-z][a-z0-9-]{0,15}$/;
const REGION_RE = /^[a-z]{2}(-[a-z]+)+-\d{1,2}$/;
const CONNECTION_RE = /^[a-zA-Z0-9\-_]{1,128}$/;

/** The admin's connect (or re-connect) form: Launchpad discovers everything else. */
function ConnectForm({ current, onConnected }: { current: ContextLayerStatus | null; onConnected: () => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [form, setForm] = useState<ContextLayerConnectInput>({
    prefix: current?.prefix ?? "",
    env: current?.env ?? "dev",
    region: current?.region ?? "",
    connection: current?.connection ?? "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const connection = (form.connection ?? "").trim();
  const valid =
    PREFIX_RE.test(form.prefix) && ENV_RE.test(form.env) && REGION_RE.test(form.region) &&
    (!connection || CONNECTION_RE.test(connection));
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.connectContextLayer({ ...form, connection: connection || null });
      toast("success", t("v2.contextLayer.connect.done"));
      onConnected();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const input = (key: keyof ContextLayerConnectInput, placeholder: string) => (
    <input
      className="v2-input mono"
      value={(form[key] as string | null | undefined) ?? ""}
      placeholder={placeholder}
      onChange={(e) => setForm((prev) => ({ ...prev, [key]: e.target.value.trim() }))}
      data-testid={`context-layer-${key}`}
    />
  );
  return (
    <div className="v2-form cols-2" data-testid="context-layer-connect">
      <Field label={t("v2.contextLayer.connect.prefix")} required hint={t("v2.contextLayer.connect.prefixHint")}>
        {input("prefix", "coa-w2")}
      </Field>
      <Field label={t("v2.contextLayer.connect.env")} required>
        {input("env", "dev")}
      </Field>
      <Field label={t("v2.contextLayer.connect.region")} required hint={t("v2.contextLayer.connect.regionHint")}>
        {input("region", "us-west-2")}
      </Field>
      <Field label={t("v2.contextLayer.connect.connection")} hint={t("v2.contextLayer.connect.connectionHint")}>
        {input("connection", `coa-${form.prefix || "<prefix>"}`)}
      </Field>
      {error && (
        <Field label="" full>
          <Alert tone="error">{error}</Alert>
        </Field>
      )}
      <Field label="" full>
        <div className="v2-row">
          <Button kind="primary" disabled={!valid || busy} onClick={() => void submit()} testId="context-layer-connect-submit">
            {busy ? t("v2.contextLayer.connect.busy") : t(current ? "v2.contextLayer.connect.reconnect" : "v2.contextLayer.connect.submit")}
          </Button>
        </div>
      </Field>
    </div>
  );
}

function HealthValue({ row }: { row: HealthRow }) {
  const { t } = useTranslation();
  const text =
    row.value === null
      ? t("v2.contextLayer.health.unknown")
      : typeof row.value === "boolean"
        ? t(row.value ? "v2.common.yes" : "v2.common.no")
        : row.value;
  return (
    <span className="v2-row" data-testid={`context-layer-health-${row.key}`} data-signal={row.signal}>
      <Lamp signal={row.signal} />
      <span className={typeof row.value === "string" ? "mono" : undefined}>{text}</span>
    </span>
  );
}

function Namespaces({ authorized }: { authorized: boolean }) {
  const { t } = useTranslation();
  const list = useLoad(
    () => (authorized ? api.listContextLayerNamespaces() : Promise.resolve({ namespaces: [] as ContextLayerNamespace[] })),
    `context-layer-namespaces:${authorized}`,
  );
  if (!authorized) return <span className="v2-muted">{t("v2.contextLayer.namespaces.needAuth")}</span>;
  return (
    <Table<ContextLayerNamespace>
      testId="context-layer-namespaces"
      rows={list.data?.namespaces ?? []}
      rowKey={(ns) => ns.namespace_id}
      loading={list.loading}
      error={list.error}
      onRetry={list.reload}
      empty={t("v2.contextLayer.namespaces.empty")}
      columns={[
        { key: "name", title: t("v2.contextLayer.namespaces.colName"), render: (ns) => namespaceLabel(ns) },
        { key: "id", title: t("v2.contextLayer.namespaces.colId"), render: (ns) => <span className="mono">{ns.namespace_id}</span> },
        { key: "status", title: t("v2.contextLayer.namespaces.colStatus"), render: (ns) => ns.status ?? "—" },
        { key: "sources", title: t("v2.contextLayer.namespaces.colSources"), render: (ns) => ns.source_count ?? "—" },
      ]}
    />
  );
}

/**
 * 上下文层 (Context layer) — the workspace's connected COA (Context Ontology
 * Accelerator) deployment: its live health, the member's own authorization and the
 * namespaces COA lets them see. Administrators connect, re-connect or disconnect
 * it. Authoring (sources, ontology, metrics, grants) stays in the COA console.
 */
export function V2ContextLayer() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { isAdmin } = useAuth();
  const status = useLoad(() => api.getContextLayer(), "context-layer");
  const [editing, setEditing] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<string[] | null>(null);
  const data = status.data;
  const auth = authStatusOf(data);

  const disconnect = async () => {
    setBusy(true);
    try {
      await api.disconnectContextLayer();
      toast("success", t("v2.contextLayer.disconnect.done"));
      setConfirm(false);
      status.reload();
    } catch (err) {
      const refs = err instanceof ApiError && err.code === "context_layer.referenced"
        ? ((err.detail as { referenced_by?: { name: string | null }[] } | null)?.referenced_by ?? [])
        : null;
      if (refs) {
        setRefused(refs.map((r) => r.name ?? t("v2.contextLayer.hiddenAgent")));
        setConfirm(false);
      } else toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const revoke = async () => {
    try {
      await api.revokeContextLayer();
      toast("success", t("v2.contextLayer.auth.revokedToast"));
      status.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    }
  };

  return (
    <>
      <PageHeader
        title={t("v2.contextLayer.title")}
        desc={t("v2.contextLayer.desc")}
        end={<Button onClick={status.reload}>{t("v2.common.refresh")}</Button>}
      />
      {status.loading && !data && <Spin />}
      {status.error && <Alert tone="error">{status.error}</Alert>}
      {data && !data.connected && (
        <Card title={t("v2.contextLayer.notConnected")} testId="context-layer-empty">
          {isAdmin ? (
            <>
              <p className="v2-muted">{t("v2.contextLayer.connect.intro")}</p>
              <ConnectForm current={null} onConnected={status.reload} />
            </>
          ) : (
            <Alert>{t("v2.contextLayer.askAdmin")}</Alert>
          )}
        </Card>
      )}
      {data?.connected && (
        <>
          <Card
            title={t("v2.contextLayer.deployment")}
            sub={data.name}
            testId="context-layer-deployment"
            end={
              data.console_url ? (
                <a className="v2-btn sm" href={data.console_url} target="_blank" rel="noopener noreferrer" data-testid="context-layer-console">
                  <ExternalLink size={13} aria-hidden="true" />
                  {t("v2.contextLayer.openConsole")}
                </a>
              ) : undefined
            }
          >
            <Descriptions
              items={[
                { label: t("v2.contextLayer.region"), value: <span className="mono">{data.region}</span> },
                { label: t("v2.contextLayer.connection"), value: <span className="mono">{data.connection}</span> },
                ...healthRows(data).map((row) => ({
                  label: t(`v2.contextLayer.health.${row.key}`),
                  value: <HealthValue row={row} />,
                })),
                { label: t("v2.contextLayer.apiEndpoint"), value: <span className="mono">{data.api_endpoint ?? "—"}</span> },
                { label: t("v2.contextLayer.connectedBy"), value: data.created_by_name ?? "—" },
                { label: t("v2.contextLayer.updatedAt"), value: fmtTime(data.updated_at ?? null) },
              ]}
            />
            {data.callback_registered === false && (
              <Alert tone="warn">
                {t("v2.contextLayer.callbackMissing")}
                {isAdmin && data.callback_url && (
                  <div className="mono" data-testid="context-layer-callback-url">
                    {data.callback_context_key}: {data.callback_url}
                  </div>
                )}
              </Alert>
            )}
            {data.agent_oauth_enabled === false && <Alert tone="warn">{t("v2.contextLayer.agentOauthOff")}</Alert>}
            {isAdmin && (
              <div className="v2-row">
                <Button size="sm" onClick={() => setEditing((v) => !v)} testId="context-layer-edit">
                  {t("v2.contextLayer.connect.reconnect")}
                </Button>
                <Button size="sm" kind="danger" onClick={() => setConfirm(true)} testId="context-layer-disconnect">
                  {t("v2.contextLayer.disconnect.action")}
                </Button>
              </div>
            )}
            {isAdmin && editing && (
              <ConnectForm
                current={data}
                onConnected={() => {
                  setEditing(false);
                  status.reload();
                }}
              />
            )}
            {refused && (
              <Alert tone="error">
                {t("v2.contextLayer.disconnect.refused", { agents: refused.join(", ") })}
              </Alert>
            )}
          </Card>
          <Card title={t("v2.contextLayer.myAuth")} sub={t("v2.contextLayer.myAuthSub")} testId="context-layer-my-auth">
            <div className="v2-row">
              <AuthorizationControl status={auth} onChanged={status.reload} />
              {auth === "authorized" && (
                <Button size="sm" kind="danger" onClick={() => void revoke()} testId="context-layer-revoke">
                  {t("v2.contextLayer.auth.revoke")}
                </Button>
              )}
              <Link to="/v2/my-connections" className="v2-link">
                {t("v2.contextLayer.auth.myConnections")}
              </Link>
            </div>
            {data.authorization?.authorized_at && (
              <p className="v2-muted">{t("v2.contextLayer.auth.since", { at: fmtTime(data.authorization.authorized_at) })}</p>
            )}
          </Card>
          <Card title={t("v2.contextLayer.namespaces.title")} sub={t("v2.contextLayer.namespaces.sub")} flush>
            <Namespaces key={`${auth}:${data.updated_at ?? ""}`} authorized={auth === "authorized"} />
          </Card>
          {(data.referenced_by?.length ?? 0) > 0 && (
            <Card title={t("v2.contextLayer.agents")} sub={String(data.referenced_by?.length ?? 0)}>
              <div className="v2-tags">
                {data.referenced_by?.map((ref, i) =>
                  ref.id ? (
                    <Link key={ref.id} to={`/v2/agents?view=detail&id=${encodeURIComponent(ref.id)}`}>
                      <Tag tone="outline">{ref.name}</Tag>
                    </Link>
                  ) : (
                    <Tag key={`hidden-${i}`} tone="gray">{t("v2.contextLayer.hiddenAgent")}</Tag>
                  ),
                )}
              </div>
            </Card>
          )}
        </>
      )}
      <Confirm
        open={confirm}
        title={t("v2.contextLayer.disconnect.title")}
        body={t("v2.contextLayer.disconnect.body", { connection: data?.connection ?? "" })}
        confirmLabel={t("v2.contextLayer.disconnect.action")}
        danger
        busy={busy}
        onConfirm={() => void disconnect()}
        onClose={() => !busy && setConfirm(false)}
      />
    </>
  );
}
