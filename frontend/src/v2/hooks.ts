import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

import { errorMessage } from "../lib/api";

export type ToastFn = (tone: "success" | "error", text: string) => void;

export const ToastContext = createContext<ToastFn>(() => undefined);

/** Transient success/error message at the top of the V2 shell. */
export function useV2Toast(): ToastFn {
  return useContext(ToastContext);
}

/** Client-side paging over an in-memory list (the backend lists are bounded). */
export function usePaged<T>(rows: T[], pageSize = 10) {
  const [page, setPage] = useState(1);
  const pages = Math.max(1, Math.ceil(rows.length / pageSize));
  const safe = Math.min(page, pages);
  const slice = useMemo(
    () => rows.slice((safe - 1) * pageSize, safe * pageSize),
    [rows, safe, pageSize],
  );
  return { page: safe, pages, setPage, slice, total: rows.length };
}

export interface Loaded<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/**
 * Fetch on mount and whenever `key` changes; `reload()` refetches. A response
 * that arrives after a newer request started is dropped, so a slow first load
 * can never overwrite a fresher one.
 */
export function useLoad<T>(fetcher: () => Promise<T>, key: string): Loaded<T> {
  const [state, setState] = useState<{ data: T | null; error: string | null; loading: boolean }>({
    data: null,
    error: null,
    loading: true,
  });
  const [nonce, setNonce] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  useEffect(() => {
    let live = true;
    setState((prev) => ({ ...prev, loading: true, error: null }));
    fetcherRef
      .current()
      .then((data) => {
        if (live) setState({ data, error: null, loading: false });
      })
      .catch((err: unknown) => {
        if (live) setState((prev) => ({ ...prev, error: errorMessage(err), loading: false }));
      });
    return () => {
      live = false;
    };
  }, [key, nonce]);
  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { ...state, reload };
}
