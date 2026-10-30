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
import { Alert, Button, Card, type Column, Confirm, Descriptions, Drawer, FilterSelect, LinkButton, Table, Tag, type TagTone } from "../../ui";
import { actorText, fmtRelevance, type RecordState, recordState, shortId, type TokenPaged, useTokenPaged } from "./common";
import { LoadMore } from "./widgets";

const TOP_K = [3, 5, 10, 20];

const STATE_TONE: Record<RecordState, TagTone> = { active: "green", superseded: "gray", deleted: "red", expired: "orange" };

function StateTag({ record }: { record: MemoryRecord }) {
  const { t } = useTranslation();
  const state = recordState(record);
  return (
    <Tag tone={STATE_TONE[state]} dot title={record.superseded_by ? `→ ${record.superseded_by}` : undefined}>
      {t(`v2.memory.state.${state}`)}
    </Tag>
  );
}

function fmtConfidence(value: number | null | undefined): string {
  return value == null ? "—" : `${Math.round(value * 100)}%`;
}

function RecordDrawer({
  record,
  strategyName,
  onClose,
  onDelete,
}: {
  record: MemoryRecord | null;
  strategyName: (id: string | null) => string;
  onClose: () => void;
  /** N10v2-bug6: asks for confirmation; absent when the record cannot be addressed (no id) */
  onDelete?: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Drawer open={record !== null} title={t("memoryPage.long.detailTitle")} onClose={onClose} testId="v2-memory-record">
      {record && (
        <div className="v2-stack" style={{ gap: 16 }}>
          {onDelete && (
            <div className="v2-row" style={{ justifyContent: "flex-end" }}>
              <Button kind="danger" onClick={onDelete} testId="v2-memory-record-delete">
                {t("memoryPage.long.delete")}
              </Button>
            </div>
          )}
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
              { label: t("v2.memory.colUpdated"), value: fmtTime(record.updated_at ?? null) },
              { label: t("v2.memory.colState"), value: <StateTag record={record} /> },
              { label: t("v2.memory.confidence"), value: fmtConfidence(record.confidence) },
              ...(record.last_op ? [{ label: t("v2.memory.lastOp"), value: <span className="mono">{record.last_op}</span> }] : []),
              ...(record.superseded_by
                ? [{ label: t("v2.memory.supersededBy"), value: <span className="mono">{record.superseded_by}</span> }]
                : []),
              ...(record.supersedes && record.supersedes.length > 0
                ? [{ label: t("v2.memory.supersedes"), value: <span className="mono">{record.supersedes.join(", ")}</span> }]
                : []),
              ...(record.score != null ? [{ label: t("v2.memory.colScore"), value: fmtRelevance(record.score) }] : []),
              ...(record.similarity != null ? [{ label: t("v2.memory.colSimilarity"), value: fmtRelevance(record.similarity) }] : []),
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
  // N10v2-bug6: the record awaiting delete confirmation, and the one being deleted
  const [pendingDelete, setPendingDelete] = useState<MemoryRecord | null>(null);
  const [deleting, setDeleting] = useState(false);
  // r14-a: superseded / deleted / expired records are hidden unless asked for
  const [showSuperseded, setShowSuperseded] = useState(false);

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
      ? (token) =>
          api.memoryRecords(
            { actor_id: actorId, strategy_id: usable.strategy_id ?? undefined, include_superseded: showSuperseded },
            token,
          )
      : null,
    `records:${actorId ?? ""}:${usable?.strategy_id ?? ""}:${showSuperseded}`,
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
  // the state column appears once any inactive row is on the page (or the toggle is on)
  const showState = showSuperseded || rows.some((r) => recordState(r) !== "active");

  // N10v2-bug6: delete is actor-scoped on the server; the console passes the actor the
  // listing was made for, so a stale row can never address another actor's record.
  const remove = async (record: MemoryRecord) => {
    if (!actorId || !record.record_id) return;
    setDeleting(true);
    try {
      await api.memoryRecordDelete(record.record_id, actorId);
      toast("success", t("memoryPage.long.deleted"));
      setPendingDelete(null);
      if (detail?.record_id === record.record_id) setDetail(null);
      if (results) setResults(results.filter((r) => r.record_id !== record.record_id));
      else records.reload();
    } catch (err: unknown) {
      toast("error", t("memoryPage.long.deleteFailed", { msg: errorMessage(err) }));
    } finally {
      setDeleting(false);
    }
  };

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
            render: (r: MemoryRecord) => (
              <span className="v2-score" title={r.similarity != null ? t("v2.memory.similarityTitle", { value: fmtRelevance(r.similarity) }) : undefined}>
                {fmtRelevance(r.score)}
              </span>
            ),
          },
        ]
      : []),
    ...(showState
      ? [
          {
            key: "state",
            title: t("v2.memory.colState"),
            width: 120,
            render: (r: MemoryRecord) => <StateTag record={r} />,
          },
        ]
      : []),
    {
      key: "created",
      title: t("v2.memory.colUpdated"),
      className: "nowrap",
      width: 170,
      // r14-a: the last write wins (an in-place `update` keeps the id but moves `updated_at`)
      render: (r) => <span title={t("v2.memory.createdTitle", { value: fmtTime(r.created_at) })}>{fmtTime(r.updated_at ?? r.created_at)}</span>,
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      width: 140,
      render: (r) => (
        <div className="v2-actions">
          <LinkButton onClick={() => setDetail(r)}>{t("v2.common.view")}</LinkButton>
          <LinkButton
            danger
            disabled={!r.record_id || deleting}
            onClick={() => setPendingDelete(r)}
            testId={`v2-memory-record-delete-${r.record_id ?? "none"}`}
          >
            {t("memoryPage.long.delete")}
          </LinkButton>
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
          <FilterSelect
            label={t("memoryPage.long.actorLabel")}
            value={actorId ?? ""}
            allLabel={t("memoryPage.long.pickActor")}
            className="v2-memory-pick"
            onChange={(v) => onSelectActor(v || null)}
            options={actors.items.map((a) => ({ value: a.actor_id, label: actorText(t, a) }))}
            testId="v2-memory-actor"
          />
          {actors.token && (
            <LinkButton disabled={actors.loading} onClick={actors.loadMore}>
              {t("v2.memory.moreActors")}
            </LinkButton>
          )}
          <FilterSelect
            label={t("memoryPage.long.strategyLabel")}
            value={strategyId ?? ""}
            allLabel={t("memoryPage.long.pickStrategy")}
            className="v2-memory-pick"
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
            <label className={`v2-check${usable ? "" : " disabled"}`} title={t("v2.memory.showSupersededHint")}>
              <input
                type="checkbox"
                checked={showSuperseded}
                disabled={!usable}
                onChange={(e) => setShowSuperseded(e.target.checked)}
                data-testid="v2-memory-show-superseded"
              />
              {t("v2.memory.showSuperseded")}
            </label>
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
      <RecordDrawer
        record={detail}
        strategyName={strategyName}
        onClose={() => setDetail(null)}
        onDelete={detail?.record_id ? () => setPendingDelete(detail) : undefined}
      />
      <Confirm
        open={pendingDelete !== null}
        title={t("memoryPage.long.deleteTitle")}
        body={
          <>
            <p>{t("memoryPage.long.deleteBody")}</p>
            {pendingDelete && (
              <p className="v2-memory-clamp" title={pendingDelete.text}>
                {pendingDelete.text}
              </p>
            )}
          </>
        }
        confirmLabel={deleting ? t("memoryPage.long.deleting") : t("memoryPage.long.delete")}
        danger
        busy={deleting}
        onConfirm={() => {
          if (pendingDelete) void remove(pendingDelete);
        }}
        onClose={() => {
          if (!deleting) setPendingDelete(null);
        }}
      />
    </>
  );
}
