/**
 * N17-obs471: the backoff pauses of the restart-window GET retry (N17-obs470, `lib/api.ts`)
 * that are waiting right now. Dependency-free so the test setup can import it without
 * loading the API client.
 *
 * A caller that may go away passes an `AbortSignal` (the registry badge does); everything
 * else is bounded by the 20 s budget. `abandonRestartRetries()` rejects every pending pause
 * with an `AbortError` — the test setup calls it after each test, so a retry never outlives
 * the test that started it (one did, and landed in the next test as a phantom request).
 */
const pending = new Set<() => void>();

export function abortError(): DOMException {
  return new DOMException("The operation was aborted.", "AbortError");
}

/** Wait `ms` unless `signal` aborts first or the pause is abandoned. */
export function restartPause(ms: number, signal?: AbortSignal | null): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(abortError());
    const stop = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", stop);
      pending.delete(stop);
      reject(abortError());
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", stop);
      pending.delete(stop);
      resolve();
    }, ms);
    signal?.addEventListener("abort", stop, { once: true });
    pending.add(stop);
  });
}

export function abandonRestartRetries(): void {
  for (const stop of [...pending]) stop();
}
