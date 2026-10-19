import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, errorMessage } from "../../../lib/api";

/** A failed COA read: the `context_layer.*` code decides how it is presented
 *  (`coaErrorKind`), the message is the localized one. */
export interface CoaError {
  code: string;
  message: string;
}

export interface CoaLoaded<T> {
  data: T | null;
  error: CoaError | null;
  loading: boolean;
  reload: () => void;
}

/**
 * `useLoad` for COA reads: it keeps the error *code* (a COA 401 must offer
 * re-consent, a 403 explain grants) rather than only the message. `enabled=false`
 * makes no request at all — a sub-page never calls COA before the member has
 * authorized Launchpad.
 */
export function useCoaLoad<T>(fetcher: () => Promise<T>, key: string, enabled = true): CoaLoaded<T> {
  const [state, setState] = useState<{ data: T | null; error: CoaError | null; loading: boolean }>({
    data: null,
    error: null,
    loading: enabled,
  });
  const [nonce, setNonce] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  useEffect(() => {
    if (!enabled) {
      setState({ data: null, error: null, loading: false });
      return;
    }
    let live = true;
    setState((prev) => ({ ...prev, loading: true, error: null }));
    fetcherRef
      .current()
      .then((data) => {
        if (live) setState({ data, error: null, loading: false });
      })
      .catch((err: unknown) => {
        if (!live) return;
        const code = err instanceof ApiError ? err.code : "network";
        setState((prev) => ({ ...prev, error: { code, message: errorMessage(err) }, loading: false }));
      });
    return () => {
      live = false;
    };
  }, [key, nonce, enabled]);
  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { ...state, reload };
}
