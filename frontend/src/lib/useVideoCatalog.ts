import { useCallback, useEffect, useState } from "react";

import { api, errorMessage } from "./api";
import type { VideoCatalog } from "./videos";

/** Published, hub-global catalog. Keep the last good snapshot during a reload failure. */
export function useVideoCatalog() {
  const [state, setState] = useState<{ data: VideoCatalog | null; loading: boolean; error: string | null }>({
    data: null,
    loading: true,
    error: null,
  });
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let live = true;
    setState((prev) => ({ ...prev, loading: true, error: null }));
    api.videoCatalog().then(
      (data) => { if (live) setState({ data, loading: false, error: null }); },
      (error: unknown) => {
        if (live) setState((prev) => ({ ...prev, loading: false, error: errorMessage(error) }));
      },
    );
    return () => { live = false; };
  }, [nonce]);
  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { ...state, reload };
}
