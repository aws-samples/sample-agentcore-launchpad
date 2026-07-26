import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Chip, DataTable, Panel, useToast } from "../../components";
import type {
  MemoryActor,
  MemoryNamespace,
  MemoryRecord,
  MemoryStrategy,
} from "../../lib/api";
import { api } from "../../lib/api";
import { score, shortId, stamp } from "./format";
import { LoadMore } from "./LoadMore";
import { usePaged } from "./paged";

interface Props {
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
 * contract stays next to `scoped_actor` instead of being re-derived here.
 */
export function LongTermTab({
  strategies,
  actorId,
  strategyId,
  onSelectActor,
  onSelectStrategy,
}: Props) {
  const { t } = useTranslation();
  const toast = useToast();

  const actors = usePaged<MemoryActor>((token) => api.memoryActors(token), []);
  const [namespaces, setNamespaces] = useState<MemoryNamespace[]>([]);
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(5);
  const [results, setResults] = useState<MemoryRecord[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [detail, setDetail] = useState<MemoryRecord | null>(null);

  useEffect(() => {
    if (!actorId) {
      setNamespaces([]);
      return;
    }
    api
      .memoryNamespaces(actorId)
      .then((res) => setNamespaces(res.items))
      .catch((err: unknown) => {
        const msg = err instanceof Error ? err.message : String(err);
        toast(t("memoryPage.loadFailed", { msg }), "crit");
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [actorId]);

  const selected = namespaces.find((n) => n.strategy_id === strategyId) ?? null;
  const usable = selected?.resolvable ? selected : null;

  const records = usePaged<MemoryRecord>(
    actorId && usable
      ? (token) =>
          api.memoryRecords(
            { actor_id: actorId, strategy_id: usable.strategy_id ?? undefined },
            token,
          )
      : null,
    [actorId, usable?.strategy_id],
  );

  // Listing and retrieval are different questions; a stale result set next to a
  // fresh listing would be misread as one ranked list.
  useEffect(() => {
    setResults(null);
  }, [actorId, strategyId]);

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
      .then((res) => {
        setResults(res.items);
        setSearching(false);
      })
      .catch((err: unknown) => {
        const msg = err instanceof Error ? err.message : String(err);
        setSearching(false);
        toast(t("memoryPage.loadFailed", { msg }), "crit");
      });
  };

  const rows = results ?? records.items;
  const isSearch = results !== null;

  return (
    <>
      <div className="filters">
        <select
          className="fsel"
          value={actorId ?? ""}
          onChange={(e) => onSelectActor(e.target.value || null)}
          aria-label={t("memoryPage.long.actorLabel")}
        >
          <option value="">{t("memoryPage.long.pickActor")}</option>
          {actors.items.map((a) => (
            <option key={a.actor_id} value={a.actor_id}>
              {a.scoped
                ? `${a.agent_name ?? t("memoryPage.short.deletedAgent")} · ${a.human_actor}`
                : a.human_actor}
            </option>
          ))}
        </select>
        <select
          className="fsel"
          value={strategyId ?? ""}
          onChange={(e) => onSelectStrategy(e.target.value || null)}
          aria-label={t("memoryPage.long.strategyLabel")}
          disabled={!actorId}
        >
          <option value="">{t("memoryPage.long.pickStrategy")}</option>
          {namespaces.map((n) => (
            <option
              key={`${n.strategy_id}-${n.template}`}
              value={n.strategy_id ?? ""}
              // {sessionId}-style templates cannot be resolved from an actor alone
              disabled={!n.resolvable}
            >
              {n.strategy_name ?? n.template}
              {n.resolvable ? "" : ` — ${t("memoryPage.long.unresolvable")}`}
            </option>
          ))}
        </select>
        {selected && (
          <span className="mono dim" title={selected.namespace}>
            {selected.namespace}
          </span>
        )}
        <span className="spacer" />
      </div>

      <div className="filters">
        <input
          className="fsearch"
          value={query}
          placeholder={t("memoryPage.long.searchPlaceholder")}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && runSearch()}
          disabled={!actorId}
        />
        <select
          className="fsel"
          value={topK}
          onChange={(e) => setTopK(Number(e.target.value))}
          aria-label={t("memoryPage.long.topKLabel")}
        >
          {[3, 5, 10, 20].map((k) => (
            <option key={k} value={k}>
              {t("memoryPage.long.topK", { k })}
            </option>
          ))}
        </select>
        <Btn primary onClick={runSearch} disabled={!actorId || !query.trim() || searching}>
          {searching ? t("common.loading") : t("memoryPage.long.search")}
        </Btn>
        {isSearch && (
          <Btn onClick={() => setResults(null)}>{t("memoryPage.long.clearSearch")}</Btn>
        )}
      </div>

      <div className="grid-2">
        <Panel
          title={
            isSearch ? t("memoryPage.long.resultsTitle") : t("memoryPage.long.recordsTitle")
          }
          sub={
            isSearch
              ? t("memoryPage.long.resultsSub", { count: rows.length })
              : selected?.namespace
          }
          end={isSearch ? <Chip tone="aqua">{t("memoryPage.long.ranked")}</Chip> : undefined}
          pad={false}
        >
          <DataTable
            columns={[
              { key: "text", label: t("memoryPage.long.colContent") },
              { key: "strategy", label: t("memoryPage.long.colStrategy") },
              ...(isSearch ? [{ key: "score", label: t("memoryPage.long.colScore") }] : []),
              { key: "created", label: t("memoryPage.long.colCreated") },
            ]}
            isEmpty={rows.length === 0}
            empty={
              !actorId
                ? t("memoryPage.long.pickActor")
                : !usable
                  ? t("memoryPage.long.pickStrategy")
                  : // Extraction is asynchronous: events can exist long before
                    // records do, so say that rather than implying "no memory".
                    t("memoryPage.long.pendingExtraction")
            }
          >
            {rows.map((r) => (
              <tr
                key={r.record_id ?? r.text}
                className={`rowlink${detail?.record_id === r.record_id ? " sel" : ""}`}
                onClick={() => setDetail(r)}
              >
                <td className="mem-cell-text">{r.text}</td>
                <td className="mono dim">{shortId(r.strategy_id, 8)}</td>
                {isSearch && <td className="mono">{score(r.score)}</td>}
                <td className="mono dim">{stamp(r.created_at)}</td>
              </tr>
            ))}
          </DataTable>
          {!isSearch && <LoadMore token={records.token} onClick={records.loadMore} />}
        </Panel>

        <Panel title={t("memoryPage.long.detailTitle")}>
          {!detail ? (
            <div className="empty">{t("memoryPage.long.pickRecord")}</div>
          ) : (
            <>
              <div className="gov-kv-list">
                <div className="kv">
                  <span className="k">{t("memoryPage.long.recordId")}</span>
                  <span className="v">{detail.record_id ?? "—"}</span>
                </div>
                <div className="kv">
                  <span className="k">{t("memoryPage.long.colStrategy")}</span>
                  <span className="v">{detail.strategy_id ?? "—"}</span>
                </div>
                <div className="kv">
                  <span className="k">{t("memoryPage.long.namespaces")}</span>
                  <span className="v">{detail.namespaces.join(", ") || "—"}</span>
                </div>
                <div className="kv">
                  <span className="k">{t("memoryPage.long.colCreated")}</span>
                  <span className="v">{stamp(detail.created_at)}</span>
                </div>
                {detail.score != null && (
                  <div className="kv">
                    <span className="k">{t("memoryPage.long.colScore")}</span>
                    <span className="v">{score(detail.score)}</span>
                  </div>
                )}
              </div>
              <p className="mem-record-body">{detail.text}</p>
              {/* Strategies that store a structured payload get their fields
                  broken out; the raw payload stays available underneath. */}
              {detail.structured && (
                <div className="gov-kv-list">
                  {Object.entries(detail.structured).map(([key, value]) => (
                    <div className="kv" key={key}>
                      <span className="k">{key}</span>
                      <span className="v">
                        {Array.isArray(value) ? value.join(", ") : String(value)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
              {detail.structured && (
                <pre className="input mono mem-raw">{detail.raw_text}</pre>
              )}
              {Object.keys(detail.metadata).length > 0 && (
                <pre className="input mono mem-raw">{JSON.stringify(detail.metadata, null, 2)}</pre>
              )}
            </>
          )}
        </Panel>
      </div>

      {strategies.length === 0 && (
        <Panel>
          <div className="empty">{t("memoryPage.overview.noStrategies")}</div>
        </Panel>
      )}
    </>
  );
}
