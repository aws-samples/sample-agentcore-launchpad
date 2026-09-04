import type { CSSProperties } from "react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { Btn, Chip, LoadError, Panel, ViewHead } from "../components";
import type { ChipTone } from "../components";
import { errorMessage, getJson } from "../lib/api";
import { CreateView } from "./knowledge/CreateView";
import { DetailView } from "./knowledge/DetailView";

export type KBStatus = "CREATING" | "ACTIVE" | "FAILED" | "DELETING";

export interface KnowledgeBaseSummary {
  kb_id: string;
  name: string;
  description: string;
  status: KBStatus | string;
  updated_at: string | null;
  data_source_count: number;
  attached_agents: string[];
}

export interface IngestionJob {
  job_id: string;
  status: string;
  started_at?: string | null;
  updated_at?: string | null;
  statistics?: Record<string, number>;
  failure_reasons?: string[];
}

export interface DataSource {
  ds_id: string;
  name: string;
  status: string;
  bucket: string | null;
  prefix?: string | null;
  failure_reasons?: string[];
  ingestion_jobs?: IngestionJob[];
}

export interface KnowledgeBaseDetail {
  kb_id: string;
  name: string;
  description: string;
  status: KBStatus | string;
  arn?: string | null;
  created_at?: string | null;
  updated_at: string | null;
  failure_reasons?: string[];
  data_sources: DataSource[];
  attached_agents: string[];
}

export interface QueryResultItem {
  text: string;
  score: number | null;
  location_uri: string | null;
  metadata: Record<string, unknown>;
}

/** Source of a KB / data source: files uploaded to the artifacts bucket, or an
 *  existing S3 location the KB service role is granted read access to. */
export type KBSourceBody = { mode: "upload" } | { mode: "existing"; bucket: string; prefix?: string };

/** KB lifecycle status → chip styling. Data-source and ingestion-job statuses
 *  (which are AWS enum values) use {@link resourceTone} and render raw. */
const KB_STATUS_CHIP: Record<string, { tone: ChipTone; icon: string; labelKey: string }> = {
  CREATING: { tone: "warn", icon: "◍", labelKey: "knowledge.status.creating" },
  ACTIVE: { tone: "good", icon: "●", labelKey: "knowledge.status.active" },
  FAILED: { tone: "crit", icon: "✕", labelKey: "knowledge.status.failed" },
  DELETING: { tone: "muted", icon: "○", labelKey: "knowledge.status.deleting" },
};

export function KbStatusChip({ status }: { status: string }) {
  const { t } = useTranslation();
  const chip = KB_STATUS_CHIP[status];
  if (!chip) return <Chip tone="muted">{status}</Chip>;
  return (
    <Chip tone={chip.tone} icon={chip.icon}>
      {t(chip.labelKey)}
    </Chip>
  );
}

export function KnowledgeBases() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const view = searchParams.get("view");
  const [items, setItems] = useState<KnowledgeBaseSummary[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const body = await getJson<{ items: KnowledgeBaseSummary[] }>("/api/knowledge-bases");
      setItems(body.items ?? []);
      setLoadError(null);
    } catch (err) {
      // never `[]` on failure — "No knowledge bases yet" is only true after a 200
      setLoadError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // ── Create sub-page (?view=create) ────────────────────────────────────────
  if (view === "create") {
    return (
      <CreateView
        onBack={() => setSearchParams({}, { replace: true })}
        onCreated={(kbId) => setSearchParams({ view: "detail", kb: kbId }, { replace: true })}
      />
    );
  }

  // ── Detail sub-page (?view=detail&kb=<id>) ────────────────────────────────
  if (view === "detail") {
    return (
      <DetailView
        kbId={searchParams.get("kb") ?? ""}
        onBack={() => {
          setSearchParams({}, { replace: true });
          void load();
        }}
      />
    );
  }

  const loading = items === null;
  const rows = items ?? [];
  const failed = loadError !== null && rows.length === 0;

  return (
    <section>
      <ViewHead
        kicker={t("knowledge.kicker")}
        title={t("knowledge.title")}
        meta={t("knowledge.meta")}
      />

      <Panel
        brk
        title={t("knowledge.list.panelTitle")}
        sub={t("knowledge.list.panelSub")}
        end={
          <Btn
            primary
            onClick={() => setSearchParams({ view: "create" })}
            data-testid="kb-create-btn"
          >
            + {t("knowledge.create.cta")}
          </Btn>
        }
        pad={false}
        style={{ "--i": 0 } as CSSProperties}
      >
        <div className="table-scroll" style={{ padding: 14 }}>
          <table>
            <thead>
              <tr>
                <th>{t("knowledge.cols.name")}</th>
                <th>{t("knowledge.cols.status")}</th>
                <th>{t("knowledge.cols.dataSources")}</th>
                <th>{t("knowledge.cols.agents")}</th>
                <th>{t("knowledge.cols.updated")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((kb) => (
                <tr
                  key={kb.kb_id}
                  onClick={() => setSearchParams({ view: "detail", kb: kb.kb_id })}
                  style={{ cursor: "pointer" }}
                  data-testid="kb-row"
                >
                  <td className="pri">
                    {kb.name}
                    {kb.description && (
                      <div className="dim" style={{ fontSize: 11, marginTop: 3, fontWeight: 400 }}>
                        {kb.description.length > 90
                          ? kb.description.slice(0, 90) + "…"
                          : kb.description}
                      </div>
                    )}
                  </td>
                  <td>
                    <KbStatusChip status={kb.status} />
                  </td>
                  <td className="mono">{kb.data_source_count}</td>
                  <td className="mono">{kb.attached_agents.length}</td>
                  <td className="mono dim">{kb.updated_at?.slice(0, 19) ?? "—"}</td>
                </tr>
              ))}
              {loading && !failed && (
                <tr>
                  <td colSpan={5} className="loading-line">
                    {t("common.loading")}
                  </td>
                </tr>
              )}
              {failed && (
                <tr>
                  <td colSpan={5}>
                    <LoadError
                      message={loadError}
                      onRetry={() => void load()}
                      inline
                      data-testid="kb-load-error"
                    />
                  </td>
                </tr>
              )}
              {!loading && !failed && rows.length === 0 && (
                <tr>
                  <td colSpan={5} style={{ padding: "26px 16px" }}>
                    <div className="note" style={{ alignItems: "flex-start" }}>
                      <span className="i">[i]</span>
                      <span>
                        <b>{t("knowledge.empty.title")}</b>
                        <br />
                        {t("knowledge.empty.body")}
                      </span>
                    </div>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Panel>
    </section>
  );
}
