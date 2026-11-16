import type { TFunction } from "i18next";
import { useEffect, useMemo, useRef, useState } from "react";

import { api, errorMessage, type MemoryActor, type MemoryNamespace, type MemoryReadScope, type MemoryRecord } from "../../../lib/api";
import { MemoryRequestScope, summaryNamespaceKey, summaryNamespaces, summaryPageStatus } from "../../../lib/memory-summary";
import { useProject } from "../../../workspace/project-context";
import { useWorkspace } from "../../../workspace/workspace-context";
import type { TagTone } from "../../ui";

/** Memory / strategy status → Tag tone (CREATING / DELETING and anything the
 *  preview API adds read as in-progress). */
export function statusTone(status: string | null | undefined): TagTone {
  const s = (status ?? "").toUpperCase();
  if (s === "ACTIVE" || s === "SUCCEEDED" || s === "COMPLETED") return "green";
  if (s === "FAILED" || s === "ERROR") return "red";
  if (!s) return "gray";
  return "blue";
}

/** Statuses that settle on their own — the resource views poll while one shows. */
export function isTransient(status: string | null | undefined): boolean {
  const s = (status ?? "").toUpperCase();
  return s === "CREATING" || s === "UPDATING" || s === "DELETING";
}

export const POLL_MS = 8000;

/** `<agent>__<human>` decoded for display; an unscoped actor is its bare id. */
export function actorText(t: TFunction, actor: MemoryActor): string {
  if (!actor.scoped) return actor.human_actor;
  return `${agentLabel(t, actor)} · ${actor.human_actor}`;
}

/** The agent half of a scoped actor; a deleted agent says so, even when its name lives on. */
export function agentLabel(t: TFunction, actor: MemoryActor): string {
  if (!actor.agent_name) return t("memoryPage.short.deletedAgent");
  return actor.agent_deleted ? t("v2.memory.deletedAgentNamed", { name: actor.agent_name }) : actor.agent_name;
}

/** Long ids (arns, 40-char session ids) shown inline; the full value goes in a title. */
export function shortId(value: string | null | undefined, keep = 12): string {
  if (!value) return "—";
  return value.length <= keep * 2 + 1 ? value : `${value.slice(0, keep)}…${value.slice(-keep)}`;
}

/** Blob payload size — memory blobs are agent state, typically well under 1 MB. */
export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** Retrieval relevance — 3 decimals is where AgentCore scores stop being noise. */
export function fmtRelevance(value: number | null | undefined): string {
  return value == null ? "—" : value.toFixed(3);
}

type Fetcher<T> = (token: string | null) => Promise<{ items: T[]; next_token: string | null }>;

export interface TokenPaged<T> {
  items: T[];
  token: string | null;
  loading: boolean;
  error: string | null;
  loadMore: () => void;
  reload: () => void;
}

/**
 * Append-paged AWS list. Every memory list operation is `nextToken`-paginated
 * (100-item page cap), so each pane keeps its own token and offers "load more"
 * instead of showing the first page as if it were everything.
 *
 * `fetchPage = null` (a prerequisite selection is missing) holds an empty,
 * non-loading state instead of firing a broken request. `key` resets the list.
 */
export function useTokenPaged<T>(fetchPage: Fetcher<T> | null, key: string): TokenPaged<T> {
  const [items, setItems] = useState<T[]>([]);
  const [token, setToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  const fetchRef = useRef(fetchPage);
  fetchRef.current = fetchPage;

  const run = (nextToken: string | null, append: boolean) => {
    const fetcher = fetchRef.current;
    const id = ++seq.current;
    if (!fetcher) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    fetcher(nextToken)
      .then((res) => {
        if (id !== seq.current) return; // stale response from a previous selection
        setItems((prev) => (append ? [...prev, ...res.items] : res.items));
        setToken(res.next_token);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (id !== seq.current) return;
        setError(errorMessage(err));
        setLoading(false);
      });
  };

  useEffect(() => {
    setItems([]);
    setToken(null);
    setError(null);
    run(null, false);
  }, [key]);

  return {
    items,
    token,
    loading,
    error,
    loadMore: () => run(token, true),
    reload: () => run(null, false),
  };
}


/** Pin sequential reads to this console's selection, not another tab's storage. */
export function useMemoryReadScope(): MemoryReadScope {
  const { current: workspace } = useWorkspace();
  const { current: project } = useProject();
  const workspaceId = workspace?.id;
  const projectId = project?.id;
  return useMemo(() => ({ workspaceId, projectId }), [workspaceId, projectId]);
}

/** Scope changes invalidate in-flight work during render, before effects run. */
export function useMemoryRequestScope(key: string): MemoryRequestScope {
  const ref = useRef<MemoryRequestScope | null>(null);
  if (ref.current === null) ref.current = new MemoryRequestScope();
  const scope = ref.current;
  scope.enter(key);
  useEffect(() => () => scope.invalidate(), [scope]);
  return scope;
}

export interface SessionSummaryView {
  status: "idle" | "loading" | "unavailable" | "pending" | "ready" | "error";
  namespaces: MemoryNamespace[];
  selected: MemoryNamespace | null;
  records: MemoryRecord[];
  token: string | null;
  error: string | null;
  warning: string | null;
  loadingMore: boolean;
  refresh: () => void;
  select: (key: string) => void;
  loadMore: () => void;
}

type SummaryData = Omit<SessionSummaryView, "refresh" | "select" | "loadMore"> & { key: string };

function emptySummary(key: string, status: SessionSummaryView["status"]): SummaryData {
  return { key, status, namespaces: [], selected: null, records: [], token: null, error: null, warning: null, loadingMore: false };
}

/** The session inspector reads an exact AWS summary namespace, never an actor prefix. */
export function useSessionSummary(actorId: string | null, sessionId: string | null): SessionSummaryView {
  const [selection, setSelection] = useState("");
  const [revision, setRevision] = useState(0);
  const readScope = useMemoryReadScope();
  const key = JSON.stringify([readScope.workspaceId, readScope.projectId, actorId, sessionId, selection, revision]);
  const scope = useMemoryRequestScope(key);
  const [data, setData] = useState<SummaryData>(() => emptySummary("", "idle"));

  useEffect(() => {
    const current = scope.begin();
    const base = emptySummary(key, actorId && sessionId ? "loading" : "idle");
    setData(base);
    if (!actorId || !sessionId || !readScope.workspaceId || !readScope.projectId) return;
    const load = async () => {
      const response = await api.memoryNamespaces(actorId, sessionId, readScope);
      const warning = response.shared_error ?? null;
      if (!current()) return;
      const namespaces = summaryNamespaces(response.items);
      const selected = namespaces.find((n) => summaryNamespaceKey(n) === selection) ?? namespaces[0] ?? null;
      setData({ ...base, warning, namespaces, selected, status: selected ? "loading" : "unavailable" });
      if (!selected) return;
      const page = await api.memoryRecords({
        actor_id: actorId, session_id: sessionId, namespace: selected.namespace,
        strategy_id: selected.strategy_id ?? undefined, record_type: "FULL_SESSION", source: selected.source,
      }, null, readScope);
      if (!current()) return;
      setData({ ...base, namespaces, selected, records: page.items, token: page.next_token,
        warning: page.shared_error ?? warning, status: summaryPageStatus(page.items) });
    };
    void load().catch((err: unknown) => {
      if (current()) setData((prev) => ({ ...prev, status: "error", error: errorMessage(err) }));
    });
  }, [actorId, sessionId, selection, key, scope, readScope]);

  // Hide an earlier session/strategy immediately, not one render after its key changes.
  const shown = data.key === key ? data : emptySummary(key, actorId && sessionId ? "loading" : "idle");
  return {
    ...shown,
    refresh: () => setRevision((n) => n + 1),
    select: setSelection,
    loadMore: () => {
      if (!actorId || !sessionId || !shown.selected || !shown.token || shown.loadingMore) return;
      const selected = shown.selected;
      const current = scope.begin();
      setData((prev) => ({ ...prev, loadingMore: true, error: null }));
      api.memoryRecords({
        actor_id: actorId, session_id: sessionId, namespace: selected.namespace,
        strategy_id: selected.strategy_id ?? undefined, record_type: "FULL_SESSION", source: selected.source,
      }, shown.token, readScope)
        .then((page) => {
          if (current()) setData((prev) => {
            const records = [...prev.records, ...page.items];
            return { ...prev, records, token: page.next_token, status: summaryPageStatus(records),
              warning: page.shared_error ?? prev.warning, loadingMore: false };
          });
        })
        .catch((err: unknown) => {
          if (current()) setData((prev) => ({ ...prev, status: "error", error: errorMessage(err), loadingMore: false }));
        });
    },
  };
}
