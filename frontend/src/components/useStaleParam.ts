import { useEffect, useRef, useState } from "react";

/**
 * Tracks a deep-link param that turned out stale. `id` is the param's current
 * value (null when absent — pass "" for "present but empty"); `stale` is the
 * caller's verdict, which must only be true once its list/detail has settled.
 * On the first stale render the id is captured for the notice and `clear` runs
 * once, so the caller strips the param (`setSearchParams(..., { replace: true })`)
 * and the effect never re-fires for the same link.
 */
export function useStaleParam(id: string | null, stale: boolean, clear: () => void) {
  const [staleId, setStaleId] = useState<string | null>(null);
  const clearRef = useRef(clear);
  useEffect(() => {
    clearRef.current = clear;
  });
  useEffect(() => {
    if (id === null || !stale) return;
    setStaleId(id);
    clearRef.current();
  }, [id, stale]);
  return { staleId, dismiss: () => setStaleId(null) };
}
