import { type ReactNode, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { errorMessage, v2KnowledgeApi } from "../../../lib/api";
import { type DataSource, formatBytes, humanizeStat, type KBDocument } from "../../../lib/knowledgeBases";
import { fmtTime } from "../../format";
import { Alert, Button, type Column, Segmented, Spin, Table } from "../../ui";
import { ResourceTag } from "./common";

const DOC_PAGE_SIZE = 50;

/** Documents of one data source — the backend page is token-based
 *  (ListKnowledgeBaseDocuments), so paging is a LOAD MORE that appends. */
function Documents({ kbId, dsId, lead }: { kbId: string; dsId: string; lead: ReactNode }) {
  const { t } = useTranslation();
  const [docs, setDocs] = useState<KBDocument[] | null>(null);
  const [nextToken, setNextToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadPage = async (token: string | null) => {
    setLoading(true);
    setError(null);
    try {
      const page = await v2KnowledgeApi.documents(kbId, dsId, DOC_PAGE_SIZE, token);
      setDocs((prev) => (token ? [...(prev ?? []), ...page.documents] : page.documents));
      setNextToken(page.next_token ?? null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadPage(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- first page once per source
  }, [kbId, dsId]);

  const columns: Column<KBDocument>[] = [
    {
      key: "name",
      title: t("knowledge.detail.sources.docName"),
      render: (d) => (
        <span className="v2-knowledge-break" title={d.uri}>
          {d.name}
        </span>
      ),
    },
    { key: "size", title: t("knowledge.detail.sources.docSize"), className: "nowrap", render: (d) => formatBytes(d.size_bytes) },
    { key: "up", title: t("knowledge.detail.sources.docUploaded"), className: "nowrap", render: (d) => fmtTime(d.uploaded_at) },
    {
      key: "status",
      title: t("knowledge.detail.sources.docStatus"),
      render: (d) => (
        <>
          <ResourceTag status={d.status} title={d.status_reason ?? undefined} />
          {d.status_reason && <span className="sub">{d.status_reason}</span>}
        </>
      ),
    },
    { key: "idx", title: t("knowledge.detail.sources.docIndexed"), className: "nowrap", render: (d) => fmtTime(d.indexed_at) },
  ];

  return (
    <>
      <div className="v2-toolbar">
        {lead}
        <Button size="sm" onClick={() => void loadPage(null)} disabled={loading} testId="v2-kb-docs-refresh">
          {t("knowledge.detail.sources.docsRefresh")}
        </Button>
        <div className="end">
          <span className="v2-count">
            {t("v2.knowledge.docsLoaded", { count: docs?.length ?? 0, more: nextToken ? "+" : "" })}
          </span>
        </div>
      </div>
      {error && <Alert tone="error">{error}</Alert>}
      <Table
        columns={columns}
        rows={docs ?? []}
        rowKey={(d) => d.uri}
        loading={loading && docs === null}
        empty={t("knowledge.detail.sources.docsEmpty")}
        density="dense"
        testId="v2-kb-docs"
      />
      {loading && docs !== null && <Spin />}
      {!loading && nextToken && (
        <div className="v2-knowledge-more">
          <Button onClick={() => void loadPage(nextToken)} testId="v2-kb-docs-more">
            {t("knowledge.detail.sources.docsLoadMore")}
          </Button>
        </div>
      )}
    </>
  );
}

function Jobs({ source, lead }: { source: DataSource; lead: ReactNode }) {
  const { t } = useTranslation();
  const jobs = source.ingestion_jobs ?? [];
  return (
    <>
      <div className="v2-toolbar">{lead}</div>
      <Table
        columns={[
          {
            key: "status",
            title: t("knowledge.cols.status"),
            render: (j) => (
              <>
                <ResourceTag status={j.status} />
                <span className="sub mono">{j.job_id}</span>
              </>
            ),
          },
          { key: "start", title: t("v2.knowledge.jobStarted"), className: "nowrap", render: (j) => fmtTime(j.started_at) },
          { key: "upd", title: t("v2.knowledge.jobUpdated"), className: "nowrap", render: (j) => fmtTime(j.updated_at) },
          {
            key: "stats",
            title: t("v2.knowledge.jobStats"),
            render: (j) =>
              j.statistics && Object.keys(j.statistics).length ? (
                <dl className="v2-knowledge-stats">
                  {Object.entries(j.statistics).map(([k, v]) => (
                    <div key={k}>
                      <dt>{humanizeStat(k)}</dt>
                      <dd>{v}</dd>
                    </div>
                  ))}
                </dl>
              ) : (
                "—"
              ),
          },
          {
            key: "fail",
            title: t("v2.knowledge.jobFailures"),
            render: (j) =>
              j.failure_reasons?.length ? (
                <span className="v2-knowledge-fail">{j.failure_reasons.join("; ")}</span>
              ) : (
                "—"
              ),
          },
        ]}
        rows={jobs}
        rowKey={(j) => j.job_id}
        empty={t("v2.knowledge.jobsEmpty")}
        density="dense"
        testId="v2-kb-jobs"
      />
    </>
  );
}

/** The selected data source: its documents (per-doc metadata) or its ingestion jobs. */
export function SourcePanel({ kbId, source }: { kbId: string; source: DataSource }) {
  const { t } = useTranslation();
  const [tab, setTab] = useState<"docs" | "jobs">("docs");
  const lead = (
    <Segmented
      value={tab}
      onChange={setTab}
      options={[
        { value: "docs", label: t("knowledge.detail.sources.documents") },
        {
          value: "jobs",
          label: `${t("v2.knowledge.jobsTab")} (${source.ingestion_jobs?.length ?? 0})`,
        },
      ]}
    />
  );
  return (
    <>
      {source.failure_reasons && source.failure_reasons.length > 0 && (
        <Alert tone="error">{source.failure_reasons.join("; ")}</Alert>
      )}
      {tab === "docs" ? (
        <Documents key={source.ds_id} kbId={kbId} dsId={source.ds_id} lead={lead} />
      ) : (
        <Jobs source={source} lead={lead} />
      )}
    </>
  );
}
