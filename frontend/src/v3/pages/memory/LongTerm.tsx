import { RefreshCw, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { ErrorCallout } from "../../../components/ErrorCallout";
import { MemoryRecordText } from "../../../components/MemoryRecordText";
import { memoryRecordPlainText } from "../../../lib/memory-record";
import { isFullSession, type MemoryRecordSelection, scopedMemoryRecord } from "../../../lib/memory-summary";
import { api, errorMessage, type MemoryActor, type MemoryNamespace, type MemoryRecord, type MemoryStrategy } from "../../../lib/api";
import { actorText, fmtRelevance, shortId, type TokenPaged, useMemoryReadScope, useMemoryRequestScope, useTokenPaged } from "../../../v2/pages/memory/common";
import { useFailure } from "../../failure";
import { ago } from "../../format";
import { Reasoned } from "../../reason";
import { Btn, Chip, Dialog, Empty, Notice, Panel, Skeleton } from "../../ui";
import { LoadMore, SourceBadge } from "./common";
import { Select } from "../../select";

const TOP_K = [3, 5, 10, 20];

function RecordDialog({ record, strategyName, onClose }: { record: MemoryRecord; strategyName: (id: string | null) => string; onClose: () => void }) {
  const { t } = useTranslation();
  return (
    <Dialog wide title={t("v3.memory.recordTitle")} onClose={onClose} foot={<Btn kind="ghost" onClick={onClose}>{t("v3.memory.close")}</Btn>}>
      <div style={{ display: "grid", gap: 14, maxHeight: "60vh", overflow: "auto" }}>
        <dl className="v3-kv">
          <dt>ID</dt><dd className="mono">{record.record_id ?? "—"}</dd>
          <dt>{t("v3.memory.strategy")}</dt><dd>{strategyName(record.strategy_id)} <span className="mono v3-mem-muted">({record.strategy_id ?? "—"})</span></dd>
          <dt>{t("v3.memory.namespace")}</dt><dd className="mono">{record.namespaces.join(", ") || "—"}</dd>
          <dt>{t("v3.memory.created")}</dt><dd>{ago(record.created_at)}</dd>
          {record.score != null && <><dt>{t("v3.memory.score")}</dt><dd className="mono">{fmtRelevance(record.score)}</dd></>}
        </dl>
        {isFullSession(record) && <Chip>{t("memoryPage.fullSummary.title")}</Chip>}
        {isFullSession(record) ? <MemoryRecordText text={record.text} />
          : <p style={{ margin: 0, whiteSpace: "pre-wrap", color: "var(--v3-text)" }}>{record.text}</p>}
        {/* a structured payload gets its fields broken out; the raw stays underneath */}
        {record.structured && (
          <>
            <dl className="v3-kv">
              {Object.entries(record.structured).map(([k, v]) => (
                <div key={k} style={{ display: "contents" }}>
                  <dt>{k}</dt>
                  <dd>{Array.isArray(v) ? v.join(", ") : typeof v === "object" && v !== null ? JSON.stringify(v) : String(v)}</dd>
                </div>
              ))}
            </dl>
            <pre className="v3-pre">{record.raw_text}</pre>
          </>
        )}
        {Object.keys(record.metadata).length > 0 && <pre className="v3-pre">{JSON.stringify(record.metadata, null, 2)}</pre>}
      </div>
    </Dialog>
  );
}

/**
 * Long-term memory = records inside a namespace. AgentCore needs a concrete
 * namespace for list and retrieve; the backend substitutes `{actorId}` into the
 * strategy template (`/api/memory/namespaces`) next to `scoped_actor`, and
 * trailing `{sessionId}` segments collapse into a prefix over every session.
 */
export function LongTerm({
  actors,
  strategies,
  actorId,
  strategyId,
  onSelectActor,
  onSelectStrategy,
}: {
  actors: TokenPaged<MemoryActor>;
  strategies: MemoryStrategy[];
  actorId: string | null;
  strategyId: string | null;
  onSelectActor: (id: string | null) => void;
  onSelectStrategy: (id: string | null) => void;
}) {
  const { t } = useTranslation();
  const failed = useFailure();
  const [namespaces, setNamespaces] = useState<MemoryNamespace[]>([]);
  const [nsError, setNsError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(5);
  const [recordType, setRecordType] = useState<"FULL_SESSION" | undefined>();
  const readScope = useMemoryReadScope();
  const requestKey = JSON.stringify([readScope.workspaceId, readScope.projectId, actorId, strategyId, recordType]);
  const requestScope = useMemoryRequestScope(requestKey);
  const [results, setResults] = useState<{ key: string; items: MemoryRecord[] } | null>(null);
  const [searching, setSearching] = useState(false);
  const [detail, setDetail] = useState<MemoryRecordSelection | null>(null);
  const shownDetail = scopedMemoryRecord(detail, requestKey);

  useEffect(() => {
    setNamespaces([]);
    setNsError(null);
    if (!actorId) return;
    let live = true;
    api.memoryNamespaces(actorId, undefined, readScope)
      .then((res) => live && setNamespaces(res.items))
      .catch((err: unknown) => live && setNsError(errorMessage(err)));
    return () => {
      live = false;
    };
  }, [actorId, readScope]);

  const selected = namespaces.find((n) => n.strategy_id === strategyId) ?? null;
  const usable = selected?.resolvable ? selected : null;
  const records = useTokenPaged<MemoryRecord>(
    actorId && usable ? (token) => api.memoryRecords({ actor_id: actorId, strategy_id: usable.strategy_id ?? undefined, record_type: recordType }, token, readScope) : null,
    `v3-records:${requestKey}:${usable?.strategy_id ?? ""}`,
  );
  // listing and retrieval answer different questions: never show a stale ranking next to a fresh list
  useEffect(() => {
    setResults(null);
    setDetail(null);
    setSearching(false);
  }, [requestKey]);

  const strategyName = useMemo(() => {
    const names = new Map<string, string>();
    for (const s of strategies) if (s.strategy_id && s.name) names.set(s.strategy_id, s.name);
    for (const n of namespaces) if (n.strategy_id && n.strategy_name) names.set(n.strategy_id, n.strategy_name);
    return (id: string | null) => (id ? (names.get(id) ?? shortId(id, 8)) : "—");
  }, [strategies, namespaces]);

  const runSearch = () => {
    if (!actorId || !query.trim()) return;
    const current = requestScope.begin();
    setSearching(true);
    api.memorySearchRecords({ query: query.trim(), actor_id: actorId, strategy_id: usable?.strategy_id ?? undefined, top_k: topK, record_type: recordType }, readScope)
      .then((res) => { if (current()) setResults({ key: requestKey, items: res.items }); })
      .catch((err: unknown) => { if (current()) failed.fail(err, t("v3.memory.loadFailed", { msg: errorMessage(err) })); })
      .finally(() => { if (current()) setSearching(false); });
  };

  const isSearch = results !== null && results.key === requestKey;
  const rows = isSearch ? results.items : records.items;
  const empty = !actorId
    ? t("v3.memory.pickActor")
    : isSearch
      ? t("v3.memory.noResults")
      : !usable
        ? t("v3.memory.pickStrategy")
        : // extraction is asynchronous: events exist long before records do
          t("v3.memory.pendingExtraction");

  return (
    <>
      <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-memory-longterm-error" />
      <Panel>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
          <Select className="v3-select" style={{ width: 280 }} value={actorId ?? ""} aria-label={t("v3.memory.actor")}
            onChange={(e) => onSelectActor(e.target.value || null)}>
            <option value="">{t("v3.memory.pickActor")}</option>
            {actors.items.map((a) => <option key={a.actor_id} value={a.actor_id}>{actorText(t, a)}</option>)}
          </Select>
          {actors.token && <Btn size="sm" kind="ghost" disabled={actors.loading} onClick={actors.loadMore}>{t("v3.memory.moreActors")}</Btn>}
          <Reasoned disabledReason={t("story.reason.pickActor")}>
            <Select className="v3-select" style={{ width: 300 }} value={strategyId ?? ""} disabled={!actorId} aria-label={t("v3.memory.strategy")}
              onChange={(e) => onSelectStrategy(e.target.value || null)}>
              <option value="">{t("v3.memory.pickStrategy")}</option>
              {/* a placeholder in the MIDDLE of the path cannot be resolved from an actor alone */}
              {namespaces.map((n) => (
                <option key={n.strategy_id ?? n.template} value={n.strategy_id ?? ""} disabled={!n.resolvable}>
                  {`${n.strategy_name ?? n.template}${n.resolvable ? (n.prefix ? ` — ${t("v3.memory.allSessions")}` : "") : ` — ${t("v3.memory.unresolvable")}`}`}
                </option>
              ))}
            </Select>
          </Reasoned>
          <Select className="v3-select" style={{ width: 220 }} value={recordType ?? ""}
            aria-label={t("memoryPage.fullSummary.filterLabel")} data-testid="v3-memory-record-type"
            onChange={(e) => setRecordType(e.target.value === "FULL_SESSION" ? "FULL_SESSION" : undefined)}>
            <option value="">{t("memoryPage.fullSummary.allRecords")}</option>
            <option value="FULL_SESSION">{t("memoryPage.fullSummary.onlyFull")}</option>
          </Select>
          {selected && <span className="mono v3-mem-muted" style={{ fontSize: 12 }} title={selected.template}>{selected.namespace}{selected.prefix ? "/…" : ""}</span>}
          <span style={{ marginLeft: "auto" }}><Btn size="sm" kind="ghost" disabled={!usable}
            disabledReason={!actorId ? t("story.reason.pickActor") : t("story.reason.pickStrategy")}
            aria-label={t("v3.memory.refresh")} onClick={records.reload}><RefreshCw size={13} /></Btn></span>
        </div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            runSearch();
          }}
          style={{ display: "flex", gap: 8, marginTop: 12 }}
        >
          <div style={{ position: "relative", flex: 1 }}>
            <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
            <Reasoned disabledReason={t("story.reason.pickActor")}>
              <input className="v3-input" style={{ paddingLeft: 34 }} value={query} disabled={!actorId} onChange={(e) => setQuery(e.target.value)}
                placeholder={t("v3.memory.searchPlaceholder")} aria-label={t("v3.memory.searchPlaceholder")} />
            </Reasoned>
          </div>
          <Select className="v3-select" style={{ width: 110 }} value={topK} onChange={(e) => setTopK(Number(e.target.value))} aria-label={t("v3.memory.topK")}>
            {TOP_K.map((k) => <option key={k} value={k}>{t("v3.memory.topKValue", { k })}</option>)}
          </Select>
          <Btn kind="primary" type="submit" busy={searching} disabled={!actorId || !query.trim()}
            disabledReason={!actorId ? t("story.reason.pickActor") : t("story.reason.typeQuery")}>{t("v3.memory.search")}</Btn>
          {isSearch && <Btn kind="ghost" onClick={() => setResults(null)}>{t("v3.memory.backToList")}</Btn>}
        </form>
      </Panel>
      {nsError && <Notice s="act">{t("v3.memory.loadFailed", { msg: nsError })}</Notice>}
      {strategies.length === 0 && <Notice s="wait">{t("v3.memory.noStrategies")}</Notice>}
      {isSearch && <Notice>{t("v3.memory.searchHint", { scope: usable ? (usable.strategy_name ?? usable.namespace) : t("v3.memory.allStrategies") })}</Notice>}

      <Panel flush title={isSearch ? t("v3.memory.results", { count: rows.length }) : t("v3.memory.loaded", { count: rows.length })}>
        {!isSearch && records.loading && rows.length === 0 ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : !isSearch && records.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{records.error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={empty} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th>{t("v3.memory.content")}</th><th>{t("v3.memory.strategy")}</th>
                {isSearch && <th className="num">{t("v3.memory.score")}</th>}
                <th className="num">{t("v3.memory.created")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r.record_id ?? `${r.text}-${i}`} className="click" onClick={() => setDetail({ key: requestKey, record: r })}>
                  <td>{isFullSession(r) && <Chip>{t("memoryPage.fullSummary.title")}</Chip>}
                    <span className="v3-mem-clamp" title={isFullSession(r) ? memoryRecordPlainText(r.text) : r.text}>
                      {isFullSession(r) ? memoryRecordPlainText(r.text) : r.text}
                    </span> <SourceBadge source={r.source} /></td>
                  <td style={{ color: "var(--v3-text-2)", whiteSpace: "nowrap" }} title={r.strategy_id ?? ""}>{strategyName(r.strategy_id)}</td>
                  {isSearch && <td className="num" style={{ color: "var(--v3-info)" }}>{fmtRelevance(r.score)}</td>}
                  <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(r.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {!isSearch && <LoadMore list={records} />}
      </Panel>
      {shownDetail && <RecordDialog record={shownDetail} strategyName={strategyName} onClose={() => setDetail(null)} />}
    </>
  );
}
