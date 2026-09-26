import type { TFunction } from "i18next";
import { useEffect, useRef, useState } from "react";

import { errorMessage, type MemoryActor } from "../../../lib/api";
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
  return `${actor.agent_name ?? t("memoryPage.short.deletedAgent")} · ${actor.human_actor}`;
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
