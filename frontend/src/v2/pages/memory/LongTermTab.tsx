import { Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  errorMessage,
  type MemoryActor,
  type MemoryNamespace,
  type MemoryRecord,
  type MemoryStrategy,
} from "../../../lib/api";
import { fmtTime } from "../../format";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Card, type Column, Descriptions, Drawer, FilterSelect, LinkButton, Table } from "../../ui";
import { actorText, fmtRelevance, shortId, type TokenPaged, useTokenPaged } from "./common";
import { FilterPick, LoadMore } from "./widgets";

const TOP_K = [3, 5, 10, 20];

function RecordDrawer({
  record,
  strategyName,
  onClose,
}: {
  record: MemoryRecord | null;
  strategyName: (id: string | null) => string;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Drawer open={record !== null} title={t("memoryPage.long.detailTitle")} onClose={onClose} testId="v2-memory-record">
      {record && (
        <div className="v2-stack" style={{ gap: 16 }}>
          <Descriptions
            one
            items={[
              { label: t("memoryPage.long.recordId"), value: <span className="mono">{record.record_id ?? "—"}</span> },
              {
                label: t("v2.memory.colStrategy"),
                value: (
                  <span title={record.strategy_id ?? ""}>
                    {strategyName(record.strategy_id)}
                    <span className="mono v2-muted"> ({record.strategy_id ?? "—"})</span>
                  </span>
                ),
              },
              {
                label: t("memoryPage.long.namespaces"),
                value: <span className="mono">{record.namespaces.join(", ") || "—"}</span>,
              },
              { label: t("v2.memory.colCreated"), value: fmtTime(record.created_at) },
              ...(record.score != null ? [{ label: t("v2.memory.colScore"), value: fmtRelevance(record.score) }] : []),
            ]}
          />
          <div>
            <div className="v2-memory-label">{t("v2.memory.colContent")}</div>
            <p className="v2-memory-body">{record.text}</p>
          </div>
          {/* Strategies that store a structured payload get their fields broken
              out; the raw payload stays available underneath. */}
          {record.structured && (
            <>
              <div>
                <div className="v2-memory-label">{t("v2.memory.structured")}</div>
                <Descriptions
                  one
                  items={Object.entries(record.structured).map(([key, value]) => ({
                    label: key,
                    value: Array.isArray(value) ? value.join(", ") : typeof value === "object" && value !== null ? JSON.stringify(value) : String(value),
                  }))}
                />
              </div>
              <div>
                <div className="v2-memory-label">{t("v2.memory.raw")}</div>
                <pre className="v2-pre">{record.raw_text}</pre>
              </div>
            </>
          )}
          {Object.keys(record.metadata).length > 0 && (
            <div>
              <div className="v2-memory-label">{t("v2.memory.metadata")}</div>
              <pre className="v2-pre">{JSON.stringify(record.metadata, null, 2)}</pre>
            </div>
          )}
        </div>
      )}
    </Drawer>
  );
}

interface Props {
  actors: TokenPaged<MemoryActor>;
  strategies: MemoryStrategy[];
  actorId: string | null;
  strategyId: string | null;
  onSelectActor: (actorId: string | null) => void;
  onSelectStrategy: (strategyId: string | null) => void;
}

/**
 * Long-term memory = records inside a namespace. AgentCore requires a concrete
 * namespace on both `ListMemoryRecords` and `RetrieveMemoryRecords`, and the
 * namespace comes from a strategy template with `{actorId}` substituted — the
 * backend does that substitution (`/api/memory/namespaces`) so the template
 * contract stays next to `scoped_actor`; trailing `{sessionId}` segments collapse
 * into a prefix covering every session of the actor.
 */
export function LongTermTab({ actors, strategies, actorId, strategyId, onSelectActor, onSelectStrategy }: Props) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [namespaces, setNamespaces] = useState<MemoryNamespace[]>([]);
  const [nsError, setNsError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(5);
  const [results, setResults] = useState<MemoryRecord[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [detail, setDetail] = useState<MemoryRecord | null>(null);

  useEffect(() => {
    setNamespaces([]);
    setNsError(null);
    if (!actorId) return;
    let live = true;
    api
      .memoryNamespaces(actorId)
      .then((res) => live && setNamespaces(res.items))
      .catch((err: unknown) => live && setNsError(errorMessage(err)));
    return () => {
      live = false;
    };
  }, [actorId]);

  const selected = namespaces.find((n) => n.strategy_id === strategyId) ?? null;
  const usable = selected?.resolvable ? selected : null;

  const records = useTokenPaged<MemoryRecord>(
    actorId && usable
      ? (token) => api.memoryRecords({ actor_id: actorId, strategy_id: usable.strategy_id ?? undefined }, token)
      : null,
    `records:${actorId ?? ""}:${usable?.strategy_id ?? ""}`,
  );

  // Listing and retrieval are different questions; a stale result set next to a
  // fresh listing would be misread as one ranked list.
  useEffect(() => {
    setResults(null);
    setDetail(null);
  }, [actorId, strategyId]);

  const strategyName = useMemo(() => {
    const names = new Map<string, string>();
    for (const s of strategies) if (s.strategy_id && s.name) names.set(s.strategy_id, s.name);
    for (const n of namespaces) if (n.strategy_id && n.strategy_name) names.set(n.strategy_id, n.strategy_name);
    return (id: string | null) => (id ? (names.get(id) ?? shortId(id, 8)) : "—");
  }, [strategies, namespaces]);

  const runSearch = () => {
    if (!actorId || !query.trim()) return;
    setSearching(true);
    api
      .memorySearchRecords({
        query: query.trim(),
        actor_id: actorId,
        strategy_id: usable?.strategy_id ?? undefined,
        top_k: topK,
      })
      .then((res) => setResults(res.items))
      .catch((err: unknown) => toast("error", t("memoryPage.loadFailed", { msg: errorMessage(err) })))
      .finally(() => setSearching(false));
  };

  const isSearch = results !== null;
  const rows = results ?? records.items;

  const columns: Column<MemoryRecord>[] = [
    {
      key: "text",
      title: t("v2.memory.colContent"),
      render: (r) => (
        <LinkButton onClick={() => setDetail(r)}>
          <span className="v2-memory-clamp" title={r.text}>
            {r.text}
          </span>
        </LinkButton>
      ),
    },
    {
      key: "strategy",
      title: t("v2.memory.colStrategy"),
      width: 180,
      render: (r) => (
        <span className="ellipsis" title={r.strategy_id ?? ""}>
          {strategyName(r.strategy_id)}
        </span>
      ),
    },
    ...(isSearch
      ? [
          {
            key: "score",
            title: t("v2.memory.colScore"),
            className: "num",
            width: 100,
            render: (r: MemoryRecord) => <span className="v2-score">{fmtRelevance(r.score)}</span>,
          },
        ]
      : []),
    { key: "created", title: t("v2.memory.colCreated"), className: "nowrap", width: 170, render: (r) => fmtTime(r.created_at) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      width: 80,
      render: (r) => (
        <div className="v2-actions">
          <LinkButton onClick={() => setDetail(r)}>{t("v2.common.view")}</LinkButton>
        </div>
      ),
    },
  ];

  const empty = !actorId
    ? t("memoryPage.long.pickActor")
    : isSearch
      ? t("v2.memory.noResults")
      : !usable
        ? t("memoryPage.long.pickStrategy")
        : // Extraction is asynchronous: events can exist long before records do,
          // so say that rather than implying "no memory".
          t("memoryPage.long.pendingExtraction");

  return (
    <>
      <Card testId="v2-memory-longterm">
        <div className="v2-toolbar">
          <FilterPick
            label={t("memoryPage.long.actorLabel")}
            value={actorId ?? ""}
            placeholder={t("memoryPage.long.pickActor")}
            onChange={(v) => onSelectActor(v || null)}
            options={actors.items.map((a) => ({ value: a.actor_id, label: actorText(t, a) }))}
            testId="v2-memory-actor"
          />
          {actors.token && (
            <LinkButton disabled={actors.loading} onClick={actors.loadMore}>
              {t("v2.memory.moreActors")}
            </LinkButton>
          )}
          <FilterPick
            label={t("memoryPage.long.strategyLabel")}
            value={strategyId ?? ""}
            placeholder={t("memoryPage.long.pickStrategy")}
            disabled={!actorId}
            onChange={(v) => onSelectStrategy(v || null)}
            // a placeholder in the MIDDLE of the path cannot be resolved from an
            // actor alone; trailing {sessionId} segments collapse into a prefix
            options={namespaces.map((n) => ({
              value: n.strategy_id ?? "",
              disabled: !n.resolvable,
              label: `${n.strategy_name ?? n.template}${
                n.resolvable ? (n.prefix ? ` — ${t("memoryPage.long.allSessions")}` : "") : ` — ${t("memoryPage.long.unresolvable")}`
              }`,
            }))}
            testId="v2-memory-strategy"
          />
          {selected && (
            <span className="mono v2-muted" title={selected.template}>
              {selected.namespace}
              {selected.prefix ? "/…" : ""}
            </span>
          )}
          <div className="end">
            <Button onClick={records.reload} disabled={!usable}>
              {t("v2.common.refresh")}
            </Button>
          </div>
        </div>
        <div className="v2-toolbar">
          <div className="v2-search v2-memory-query">
            <Search size={14} aria-hidden="true" />
            <input
              className="v2-input"
              value={query}
              placeholder={t("memoryPage.long.searchPlaceholder")}
              aria-label={t("memoryPage.long.searchPlaceholder")}
              disabled={!actorId}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && runSearch()}
              data-testid="v2-memory-query"
            />
          </div>
          <FilterSelect
            label={t("memoryPage.long.topKLabel")}
            value={String(topK)}
            onChange={(v) => setTopK(Number(v))}
            options={TOP_K.map((k) => ({ value: String(k), label: t("memoryPage.long.topK", { k }) }))}
          />
          <Button kind="primary" disabled={!actorId || !query.trim() || searching} onClick={runSearch} testId="v2-memory-search">
            {searching ? t("v2.common.loading") : t("v2.memory.search")}
          </Button>
          {isSearch && <Button onClick={() => setResults(null)}>{t("v2.memory.backToList")}</Button>}
          <div className="end">
            <span className="v2-count">
              {isSearch ? t("memoryPage.long.resultsSub", { count: rows.length }) : t("v2.memory.loaded", { count: rows.length })}
            </span>
          </div>
        </div>
        {nsError && <Alert tone="error">{t("memoryPage.loadFailed", { msg: nsError })}</Alert>}
        {isSearch && (
          <Alert tone="info">
            {t("v2.memory.searchHint", { scope: usable ? (usable.strategy_name ?? usable.namespace) : t("v2.memory.allStrategies") })}
          </Alert>
        )}
        <Table
          columns={columns}
          rows={rows}
          rowKey={(r) => r.record_id ?? r.text}
          loading={!isSearch && records.loading}
          error={isSearch ? null : records.error}
          onRetry={records.reload}
          empty={empty}
          selectedKey={detail?.record_id ?? null}
          testId="v2-memory-records"
        />
        {!isSearch && <LoadMore token={records.token} loading={records.loading} onClick={records.loadMore} testId="v2-memory-records-more" />}
        {strategies.length === 0 && <Alert tone="warn">{t("memoryPage.overview.noStrategies")}</Alert>}
      </Card>
      <RecordDrawer record={detail} strategyName={strategyName} onClose={() => setDetail(null)} />
    </>
  );
}
