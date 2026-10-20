import { MessageSquareText } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import {
  api,
  ApiError,
  type ContextLayerNamespace,
  type ContextLayerStatus,
  errorMessage,
  localizedMessage,
} from "../../../lib/api";
import { fmtTime } from "../../format";
import { type Loaded, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, Descriptions, Lamp, LinkButton, PageHeader, Spin, Table, Tag } from "../../ui";
import { AuthorizationControl } from "./AuthorizationControl";
import { CoaErrorNotice, ConsoleLink, StatusLamp } from "./common";
import { ConnectForm } from "./ConnectForm";
import { GatewayCard } from "./GatewayCard";
import { MyAuthorization } from "./MyAuthorization";
import { authStatusOf, healthRows, type HealthRow, namespaceLabel } from "./state";
import { useCoaLoad } from "./useCoaLoad";
import { coaConsoleUrl, type ContextView, healthSignal, namespaceSignal, namespacesState } from "./views";

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

/** COA's own dependency health (`/system-health`, read as the member). */
function CoaServices({ onAuthorized }: { onAuthorized: () => void }) {
  const { t } = useTranslation();
  const health = useCoaLoad(() => api.getContextLayerHealth(), "context-layer-health");
  const coa = health.data?.coa ?? null;
  return (
    <Card
      title={t("v2.contextLayer.services.title")}
      sub={t("v2.contextLayer.services.sub")}
      end={coa?.status ? <StatusLamp signal={healthSignal(coa.status)} status={coa.status} testId="context-layer-services-status" /> : undefined}
      testId="context-layer-services"
    >
      {health.loading && !health.data && <Spin />}
      {health.error && <CoaErrorNotice error={health.error} onAuthorized={onAuthorized} onRetry={health.reload} />}
      {health.data?.coa_error && (
        <CoaErrorNotice
          error={{ code: health.data.coa_error.code, message: localizedMessage(health.data.coa_error.code, health.data.coa_error.code) }}
          onAuthorized={onAuthorized}
          onRetry={health.reload}
        />
      )}
      {coa && (
        <Descriptions
          items={coa.components.map((c) => ({
            label: t(`v2.contextLayer.services.${c.key}`, { defaultValue: c.key }),
            value: <StatusLamp signal={healthSignal(c.status)} status={c.status} testId={`context-layer-service-${c.key}`} />,
          }))}
        />
      )}
    </Card>
  );
}

function Namespaces({
  auth,
  consoleUrl,
  onAuthorized,
  go,
}: {
  auth: ReturnType<typeof authStatusOf>;
  consoleUrl: string | null;
  onAuthorized: () => void;
  go: (view: ContextView) => void;
}) {
  const { t } = useTranslation();
  const authorized = auth === "authorized";
  const list = useCoaLoad(() => api.listContextLayerNamespaces(), `context-layer-namespaces:${auth}`, authorized);
  const state = namespacesState(auth, { data: list.data?.namespaces ?? null, error: list.error, loading: list.loading });
  if (state === "needAuth") {
    return (
      <div className="v2-cl-empty" data-testid="context-layer-namespaces-auth">
        <p className="v2-muted">{t("v2.contextLayer.namespaces.needAuth")}</p>
        <AuthorizationControl status={auth} onChanged={onAuthorized} testId="context-layer-namespaces-authorize" />
      </div>
    );
  }
  if (state === "error" && list.error) {
    return (
      <div className="v2-cl-empty">
        <CoaErrorNotice error={list.error} onAuthorized={onAuthorized} onRetry={list.reload} consoleUrl={consoleUrl} />
      </div>
    );
  }
  if (state === "empty") {
    return (
      <div className="v2-cl-empty" data-testid="context-layer-namespaces-empty">
        <p>{t("v2.contextLayer.namespaces.empty")}</p>
        <p className="v2-muted">{t("v2.contextLayer.namespaces.emptyHint")}</p>
        <ConsoleLink href={coaConsoleUrl(consoleUrl, "home")} label={t("v2.contextLayer.openConsole")} />
      </div>
    );
  }
  return (
    <Table<ContextLayerNamespace>
      testId="context-layer-namespaces"
      rows={list.data?.namespaces ?? []}
      rowKey={(ns) => ns.namespace_id}
      loading={state === "loading"}
      columns={[
        {
          key: "name",
          title: t("v2.contextLayer.namespaces.colName"),
          render: (ns) => (
            <>
              <LinkButton onClick={() => go({ view: "namespace", id: ns.namespace_id, tab: "sources" })} testId={`context-layer-ns-${ns.name}`}>
                {namespaceLabel(ns)}
              </LinkButton>
              {ns.display_name && ns.name && ns.display_name !== ns.name && <span className="sub mono">{ns.name}</span>}
            </>
          ),
        },
        { key: "id", title: t("v2.contextLayer.namespaces.colId"), render: (ns) => <span className="mono">{ns.namespace_id}</span> },
        {
          key: "status",
          title: t("v2.contextLayer.namespaces.colStatus"),
          render: (ns) => <StatusLamp signal={namespaceSignal(ns.status)} status={ns.status} />,
        },
        { key: "sources", title: t("v2.contextLayer.namespaces.colSources"), render: (ns) => <span className="mono">{ns.source_count ?? "—"}</span> },
        {
          key: "ops",
          title: t("v2.common.actions"),
          className: "right",
          render: (ns) => (
            <div className="v2-actions">
              <LinkButton onClick={() => go({ view: "namespace", id: ns.namespace_id, tab: "sources" })}>{t("v2.contextLayer.namespaces.browse")}</LinkButton>
              <LinkButton onClick={() => go({ view: "playground", ns: ns.namespace_id })} testId={`context-layer-ns-play-${ns.name}`}>
                {t("v2.contextLayer.playground.title")}
              </LinkButton>
            </div>
          ),
        },
      ]}
    />
  );
}

/**
 * The module's landing view: the connected COA deployment and its live health,
 * the member's own authorization, COA's services and the namespaces COA lets them
 * see. Administrators connect, re-connect or disconnect the deployment here.
 */
export function Overview({ status, go }: { status: Loaded<ContextLayerStatus>; go: (view: ContextView) => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { isAdmin } = useAuth();
  const [editing, setEditing] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<string[] | null>(null);
  const data = status.data;
  const auth = authStatusOf(data);
  const consoleUrl = data?.console_url ?? null;

  const disconnect = async () => {
    setBusy(true);
    try {
      await api.disconnectContextLayer();
      toast("success", t("v2.contextLayer.disconnect.done"));
      setConfirm(false);
      status.reload();
    } catch (err) {
      const refs =
        err instanceof ApiError && err.code === "context_layer.referenced"
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
  return (
    <>
      <PageHeader
        title={t("v2.contextLayer.title")}
        desc={t("v2.contextLayer.desc")}
        end={
          <>
            <Button onClick={status.reload}>{t("v2.common.refresh")}</Button>
            {data?.connected && (
              <Button kind="primary" onClick={() => go({ view: "playground", ns: null })} testId="context-layer-open-playground">
                <MessageSquareText size={14} aria-hidden="true" />
                {t("v2.contextLayer.playground.title")}
              </Button>
            )}
          </>
        }
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
            end={<ConsoleLink href={coaConsoleUrl(consoleUrl, "home")} label={t("v2.contextLayer.openConsole")} testId="context-layer-console" />}
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
            {refused && <Alert tone="error">{t("v2.contextLayer.disconnect.refused", { agents: refused.join(", ") })}</Alert>}
          </Card>
          <GatewayCard layer={data} onChanged={status.reload} />
          <Card title={t("v2.contextLayer.myAuth")} sub={t("v2.contextLayer.myAuthSub")} testId="context-layer-my-auth">
            <MyAuthorization status={data} onChanged={status.reload} />
          </Card>
          <Card title={t("v2.contextLayer.namespaces.title")} sub={t("v2.contextLayer.namespaces.sub")} flush testId="context-layer-namespaces-card">
            <Namespaces key={`${auth}:${data.updated_at ?? ""}`} auth={auth} consoleUrl={consoleUrl} onAuthorized={status.reload} go={go} />
          </Card>
          {auth === "authorized" && <CoaServices key={data.updated_at ?? ""} onAuthorized={status.reload} />}
          {(data.referenced_by?.length ?? 0) > 0 && (
            <Card title={t("v2.contextLayer.agents")} sub={String(data.referenced_by?.length ?? 0)}>
              <div className="v2-tags">
                {data.referenced_by?.map((ref, i) =>
                  ref.id ? (
                    <Link key={ref.id} to={`/v2/agents?view=detail&id=${encodeURIComponent(ref.id)}`}>
                      <Tag tone="outline">{ref.name}</Tag>
                    </Link>
                  ) : (
                    <Tag key={`hidden-${i}`} tone="gray">
                      {t("v2.contextLayer.hiddenAgent")}
                    </Tag>
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
