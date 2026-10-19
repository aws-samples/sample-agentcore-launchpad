import { Link2, MessageSquareText } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  type ContextLayerMetric,
  type ContextLayerOntologyClass,
  type ContextLayerSource,
  type ContextLayerStatus,
} from "../../../lib/api";
import { fmtTime } from "../../format";
import type { Loaded } from "../../hooks";
import { Alert, Button, Card, Descriptions, FlowHeader, Lamp, SearchInput, Spin, SubTabs, Table, Tag } from "../../ui";
import { AttachDialog } from "./AttachDialog";
import { CoaErrorNotice, ConsoleLink, GateNotice, StatusLamp } from "./common";
import { namespaceLabel } from "./state";
import { useCoaLoad } from "./useCoaLoad";
import {
  coaConsoleUrl,
  coaErrorKind,
  type ContextView,
  moduleGate,
  NAMESPACE_TABS,
  type NamespaceTab,
  namespaceSignal,
  ontologySignal,
  sourceSignal,
} from "./views";

interface TabProps {
  id: string;
  consoleUrl: string | null;
  onAuthorized: () => void;
}

function SourcesTab({ id, consoleUrl, onAuthorized }: TabProps) {
  const { t } = useTranslation();
  const list = useCoaLoad(() => api.listContextLayerSources(id), `context-layer-sources:${id}`);
  return (
    <Card
      title={t("v2.contextLayer.sources.title")}
      sub={t("v2.contextLayer.sources.sub")}
      end={<ConsoleLink href={coaConsoleUrl(consoleUrl, "sources", id)} testId="context-layer-coa-sources" />}
      flush
      testId="context-layer-sources"
    >
      {list.error && coaErrorKind(list.error.code) !== "other" ? (
        <div className="v2-cl-empty">
          <CoaErrorNotice error={list.error} onAuthorized={onAuthorized} consoleUrl={consoleUrl} />
        </div>
      ) : (
        <Table<ContextLayerSource>
          testId="context-layer-sources-table"
          rows={list.data?.sources ?? []}
          rowKey={(s) => s.source_id}
          loading={list.loading}
          error={list.error?.message ?? null}
          onRetry={list.reload}
          empty={t("v2.contextLayer.sources.empty")}
          columns={[
            {
              key: "name",
              title: t("v2.contextLayer.sources.colName"),
              render: (s) => (
                <>
                  <span>{s.name || s.source_id}</span>
                  <span className="sub mono">{s.source_id}</span>
                </>
              ),
            },
            {
              key: "type",
              title: t("v2.contextLayer.sources.colType"),
              render: (s) => (
                <>
                  <span className="mono">{s.source_type ?? "—"}</span>
                  {s.source_subtype && <span className="sub mono">{s.source_subtype}</span>}
                </>
              ),
            },
            {
              key: "status",
              title: t("v2.contextLayer.sources.colStatus"),
              render: (s) => <StatusLamp signal={sourceSignal(s.status)} status={s.status} />,
            },
            {
              key: "tables",
              title: t("v2.contextLayer.sources.colTables"),
              render: (s) => <span className="mono">{s.tables_discovered ?? "—"}</span>,
            },
            {
              key: "updated",
              title: t("v2.contextLayer.sources.colUpdated"),
              className: "nowrap",
              render: (s) => fmtTime(s.updated_at ?? s.created_at),
            },
          ]}
        />
      )}
    </Card>
  );
}

function MetricsTab({ id, consoleUrl, onAuthorized }: TabProps) {
  const { t } = useTranslation();
  const list = useCoaLoad(() => api.listContextLayerMetrics(id), `context-layer-metrics:${id}`);
  return (
    <Card
      title={t("v2.contextLayer.metrics.title")}
      sub={t("v2.contextLayer.metrics.sub")}
      end={<ConsoleLink href={coaConsoleUrl(consoleUrl, "metrics", id)} testId="context-layer-coa-metrics" />}
      flush
      testId="context-layer-metrics"
    >
      {list.error && coaErrorKind(list.error.code) !== "other" ? (
        <div className="v2-cl-empty">
          <CoaErrorNotice error={list.error} onAuthorized={onAuthorized} consoleUrl={consoleUrl} />
        </div>
      ) : (
        <Table<ContextLayerMetric>
          testId="context-layer-metrics-table"
          rows={list.data?.metrics ?? []}
          rowKey={(m) => m.name}
          loading={list.loading}
          error={list.error?.message ?? null}
          onRetry={list.reload}
          empty={t("v2.contextLayer.metrics.empty")}
          columns={[
            {
              key: "name",
              title: t("v2.contextLayer.metrics.colName"),
              render: (m) => (
                <>
                  <span className="mono">{m.name}</span>
                  {m.description && <span className="sub v2-cl-desc">{m.description}</span>}
                </>
              ),
            },
            { key: "unit", title: t("v2.contextLayer.metrics.colUnit"), render: (m) => m.unit ?? "—" },
            {
              key: "synonyms",
              title: t("v2.contextLayer.metrics.colSynonyms"),
              render: (m) =>
                m.synonyms.length ? (
                  <div className="v2-tags">
                    {m.synonyms.map((s) => (
                      <Tag key={s} tone="outline">
                        {s}
                      </Tag>
                    ))}
                  </div>
                ) : (
                  "—"
                ),
            },
            {
              key: "dialects",
              title: t("v2.contextLayer.metrics.colDialects"),
              render: (m) =>
                m.dialects.length ? (
                  <details className="v2-cl-expr" data-testid={`context-layer-metric-expr-${m.name}`}>
                    <summary>
                      {m.dialects.map((d) => (
                        <Tag key={d.dialect} tone="blue">
                          {d.dialect}
                        </Tag>
                      ))}
                    </summary>
                    {m.dialects.map((d) => (
                      <pre key={d.dialect} className="mono">
                        {d.expression}
                      </pre>
                    ))}
                  </details>
                ) : (
                  "—"
                ),
            },
            {
              key: "table",
              title: t("v2.contextLayer.metrics.colTable"),
              render: (m) => <span className="mono">{m.source_table ?? "—"}</span>,
            },
          ]}
        />
      )}
    </Card>
  );
}

function OntologyTab({ id, consoleUrl, onAuthorized }: TabProps) {
  const { t } = useTranslation();
  const view = useCoaLoad(() => api.getContextLayerOntology(id), `context-layer-ontology:${id}`);
  const [query, setQuery] = useState("");
  const classes = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const all = view.data?.classes ?? [];
    if (!needle) return all;
    return all.filter((c) => `${c.label}\n${c.uri}\n${c.description ?? ""}\n${c.ontology}`.toLowerCase().includes(needle));
  }, [view.data, query]);
  const ontologies = view.data?.ontologies ?? [];
  return (
    <Card
      title={t("v2.contextLayer.ontology.title")}
      sub={t("v2.contextLayer.ontology.sub")}
      end={<ConsoleLink href={coaConsoleUrl(consoleUrl, "ontology", id)} testId="context-layer-coa-ontology" />}
      testId="context-layer-ontology"
    >
      {view.error && coaErrorKind(view.error.code) !== "other" ? (
        <CoaErrorNotice error={view.error} onAuthorized={onAuthorized} consoleUrl={consoleUrl} />
      ) : (
        <>
          {ontologies.length > 0 && (
            <div className="v2-tags v2-cl-ontologies" data-testid="context-layer-ontologies">
              {ontologies.map((o) => (
                <Tag key={o.ontology_id} tone="outline" title={o.description ?? o.ontology_id}>
                  <span className="v2-row" data-signal={ontologySignal(o.status)}>
                    <Lamp signal={ontologySignal(o.status)} title={o.status ?? undefined} />
                    <span>{o.title}</span>
                    {o.ontology_type && <span className="v2-muted">{o.ontology_type}</span>}
                    {o.class_count != null && <span className="v2-muted">{t("v2.contextLayer.ontology.classCount", { count: o.class_count })}</span>}
                  </span>
                </Tag>
              ))}
            </div>
          )}
          {(view.data?.failed.length ?? 0) > 0 && (
            <Alert tone="warn">{t("v2.contextLayer.ontology.failed", { count: view.data?.failed.length ?? 0 })}</Alert>
          )}
          {view.data?.truncated && <Alert>{t("v2.contextLayer.ontology.truncated")}</Alert>}
          {(view.data?.classes.length ?? 0) > 0 && (
            <div className="v2-cl-toolbar">
              <SearchInput
                value={query}
                onChange={setQuery}
                placeholder={t("v2.contextLayer.ontology.search")}
                testId="context-layer-ontology-search"
              />
            </div>
          )}
          <Table<ContextLayerOntologyClass>
            testId="context-layer-classes"
            rows={classes}
            rowKey={(c) => `${c.ontology_id}|${c.uri}`}
            loading={view.loading}
            error={view.error?.message ?? null}
            onRetry={view.reload}
            empty={query ? t("v2.common.noMatch") : t("v2.contextLayer.ontology.empty")}
            columns={[
              {
                key: "label",
                title: t("v2.contextLayer.ontology.colClass"),
                render: (c) => (
                  <>
                    <span className="mono">{c.label}</span>
                    <span className="sub mono v2-cl-break">{c.uri}</span>
                  </>
                ),
              },
              {
                key: "description",
                title: t("v2.contextLayer.ontology.colDescription"),
                render: (c) => (c.description ? <span className="v2-cl-desc">{c.description}</span> : "—"),
              },
              { key: "ontology", title: t("v2.contextLayer.ontology.colOntology"), render: (c) => c.ontology || "—" },
            ]}
          />
        </>
      )}
    </Card>
  );
}

/**
 * `?view=namespace&id=<uuid>[&tab=]` — one COA namespace as COA shows it to the
 * member: its sources, governed metrics and ontology classes, each with a deep
 * link into the COA console (authoring stays there), plus the playground and
 * "attach to agent".
 */
export function NamespaceView({
  id,
  tab,
  status,
  go,
}: {
  id: string;
  tab: NamespaceTab;
  status: Loaded<ContextLayerStatus>;
  go: (view: ContextView, replace?: boolean) => void;
}) {
  const { t } = useTranslation();
  const gate = moduleGate(status);
  const consoleUrl = status.data?.console_url ?? null;
  const ns = useCoaLoad(() => api.getContextLayerNamespace(id), `context-layer-ns:${id}`, gate === "ready");
  const [attach, setAttach] = useState(false);
  const back = () => go({ view: "overview" });
  const detail = ns.data?.namespace ?? null;
  const label = detail ? namespaceLabel(detail) : id;

  if (gate !== "ready") {
    return (
      <>
        <FlowHeader title={id} onBack={back} />
        <GateNotice gate={gate} status={status.data} error={status.error} onChanged={status.reload} onOverview={back} />
      </>
    );
  }
  const blocked = ns.error ? coaErrorKind(ns.error.code) : null;
  const showTabs = !blocked || blocked === "other";
  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            {label}
            {detail?.status && <StatusLamp signal={namespaceSignal(detail.status)} status={detail.status} />}
          </span>
        }
        onBack={back}
        end={
          <>
            <ConsoleLink href={coaConsoleUrl(consoleUrl, "namespace", id)} testId="context-layer-coa-namespace" />
            <Button onClick={() => go({ view: "playground", ns: id })} testId="context-layer-ns-playground">
              <MessageSquareText size={14} aria-hidden="true" />
              {t("v2.contextLayer.playground.title")}
            </Button>
            <Button kind="primary" disabled={!detail} onClick={() => setAttach(true)} testId="context-layer-attach-open">
              <Link2 size={14} aria-hidden="true" />
              {t("v2.contextLayer.attach.action")}
            </Button>
          </>
        }
      />
      {ns.loading && !detail && <Spin />}
      {ns.error && <CoaErrorNotice error={ns.error} onAuthorized={() => { status.reload(); ns.reload(); }} onRetry={ns.reload} consoleUrl={consoleUrl} />}
      {detail && (
        <Card title={t("v2.contextLayer.namespace.basic")} testId="context-layer-namespace">
          <Descriptions
            items={[
              { label: t("v2.contextLayer.namespaces.colName"), value: detail.name || "—" },
              { label: t("v2.contextLayer.namespaces.colId"), value: <span className="mono">{detail.namespace_id}</span> },
              { label: t("v2.contextLayer.namespaces.colStatus"), value: <StatusLamp signal={namespaceSignal(detail.status)} status={detail.status} /> },
              { label: t("v2.contextLayer.namespace.owner"), value: detail.owner ?? "—" },
              { label: t("v2.contextLayer.namespaces.colSources"), value: <span className="mono">{detail.source_count ?? "—"}</span> },
              { label: t("v2.common.createdAt"), value: fmtTime(detail.created_at) },
              { label: t("v2.contextLayer.updatedAt"), value: fmtTime(detail.updated_at) },
              { label: t("v2.contextLayer.namespace.description"), value: detail.description || "—" },
            ]}
          />
        </Card>
      )}
      {showTabs && (
        <>
          <div className="v2-cl-tabs">
            <SubTabs
              value={tab}
              onChange={(next) => go({ view: "namespace", id, tab: next }, true)}
              tabs={NAMESPACE_TABS.map((value) => ({ value, label: t(`v2.contextLayer.tabs.${value}`) }))}
            />
          </div>
          {tab === "sources" && <SourcesTab id={id} consoleUrl={consoleUrl} onAuthorized={status.reload} />}
          {tab === "metrics" && <MetricsTab id={id} consoleUrl={consoleUrl} onAuthorized={status.reload} />}
          {tab === "ontology" && <OntologyTab id={id} consoleUrl={consoleUrl} onAuthorized={status.reload} />}
        </>
      )}
      {attach && detail && <AttachDialog ns={{ id, name: label }} onClose={() => setAttach(false)} />}
    </>
  );
}
