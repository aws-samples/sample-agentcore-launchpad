import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { useToast } from "../../components";

type Fetcher<T> = (token: string | null) => Promise<{ items: T[]; next_token: string | null }>;

/**
 * Append-paged AWS list.
 *
 * Every memory list operation is `nextToken`-paginated with a 100-item page cap,
 * so each pane keeps its own token and exposes "load more" instead of silently
 * showing the first page as if it were everything.
 *
 * Pass `null` as the fetcher when a prerequisite selection is missing — the hook
 * then holds an empty, non-loading state rather than firing a broken request.
 */
export function usePaged<T>(fetchPage: Fetcher<T> | null, deps: unknown[]) {
  const [items, setItems] = useState<T[]>([]);
  const [token, setToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  const toast = useToast();
  const { t } = useTranslation();

  const run = (nextToken: string | null, append: boolean) => {
    if (!fetchPage) return;
    const id = ++seq.current;
    setLoading(true);
    setError(null);
    fetchPage(nextToken)
      .then((res) => {
        if (id !== seq.current) return; // stale response from a previous selection
        setItems((prev) => (append ? [...prev, ...res.items] : res.items));
        setToken(res.next_token);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (id !== seq.current) return;
        const msg = err instanceof Error ? err.message : String(err);
        setError(msg);
        setLoading(false);
        toast(t("memoryPage.loadFailed", { msg }), "crit");
      });
  };

  useEffect(() => {
    setItems([]);
    setToken(null);
    setError(null);
    run(null, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return {
    items,
    token,
    loading,
    error,
    loadMore: () => run(token, true),
    reload: () => run(null, false),
  };
}

