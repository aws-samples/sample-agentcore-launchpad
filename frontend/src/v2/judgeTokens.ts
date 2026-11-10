/**
 * N17-obs436: one meaning of a judge's "input" tokens across the console. Bedrock's usage
 * `inputTokens` leaves out prompt-cache reads (the judge model caches a repeated prompt prefix),
 * while `totalTokens` counts them — so the judge's whole prompt is `total − output`; the cached
 * share is `prompt − inputTokens`. Without a total (or a total that adds nothing) the reported
 * input stands.
 */
export interface JudgeTokenSplit {
  /** every prompt token the judge read (cache reads included) */
  prompt: number;
  /** what Bedrock reports as `inputTokens` — the uncached part */
  uncached: number;
  cached: number;
  output: number;
}

export function judgeTokens(input: number | null | undefined, output: number | null | undefined,
                            total: number | null | undefined): JudgeTokenSplit {
  const uncached = input ?? 0;
  const out = output ?? 0;
  const prompt = total != null && total - out > uncached ? total - out : uncached;
  return { prompt, uncached, cached: prompt - uncached, output: out };
}

/** N17-obs439: a span / trace / session / model token block (observability). `input` is the
 *  providers' uncached prompt; the prompt cache is reported beside it (`cache_read`,
 *  `cache_write`) and the backend's `total` adds all four. Without the cache fields it falls
 *  back to the judge rule (total − output). */
export interface TokenCounts {
  input?: number | null;
  output?: number | null;
  total?: number | null;
  cache_read?: number | null;
  cache_write?: number | null;
}

export interface TokenSplit extends JudgeTokenSplit {
  read: number;
  write: number;
}

export function tokenSplit(counts: TokenCounts): TokenSplit {
  const uncached = counts.input ?? 0;
  const read = counts.cache_read ?? 0;
  const write = counts.cache_write ?? 0;
  if (counts.cache_read != null || counts.cache_write != null) {
    const prompt = uncached + read + write;
    return { prompt, uncached, cached: read + write, output: counts.output ?? 0, read, write };
  }
  return { ...judgeTokens(counts.input, counts.output, counts.total), read: 0, write: 0 };
}

/** The tooltip that explains a prompt count with a cached share (`undefined` when none). */
export function cacheHint(
  t: (key: string, options?: Record<string, unknown>) => string,
  split: TokenSplit,
  fmt: (n: number) => string,
): string | undefined {
  if (split.cached <= 0) return undefined;
  return t("v2.observability.tokensCacheHint", {
    prompt: fmt(split.prompt), uncached: fmt(split.uncached), read: fmt(split.read), write: fmt(split.write),
  });
}
