/**
 * All-pairs (pairwise) combination generator for the round-16 UI combination suites.
 *
 * Greedy AETG-style: every candidate row is built factor by factor, picking the value that
 * covers the most still-uncovered pairs while a caller `constraint` keeps the partial row
 * legal; the best of `candidates` rows per round is kept. Deterministic for a seed, so a
 * failing combination can be reproduced by its index. Pure TypeScript (type-strip safe:
 * no enums, no parameter properties) so `node --test` runs the unit test without a build.
 */

export interface Factor {
  name: string;
  values: readonly string[];
}

export type Combo = Record<string, string>;

/** `true` when a (possibly partial) row is legal. Must be monotone: a partial row that
 *  fails can never be completed into a passing one. */
export type Constraint = (partial: Combo) => boolean;

export interface PairwiseOptions {
  constraint?: Constraint;
  seed?: number;
  /** candidate rows weighed per round (more = smaller suites, slower) */
  candidates?: number;
}

export interface PairwiseResult {
  combos: Combo[];
  /** feasible pairs (pass the constraint on their own) the greedy search could not place
   *  into any legal full row — a constraint tighter than its pairs reveal */
  infeasiblePairs: Pair[];
  /** pairs the suite had to cover (constraint-feasible) */
  pairCount: number;
  /** size of the full cartesian product after constraints */
  fullProductSize: number;
}

export interface Pair {
  a: [string, string];
  b: [string, string];
}

/** mulberry32 — a small seeded RNG so a run is reproducible from its seed. */
export function rng(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const pairKey = (fa: string, va: string, fb: string, vb: string) =>
  fa < fb ? `${fa}=${va}\u0000${fb}=${vb}` : `${fb}=${vb}\u0000${fa}=${va}`;

/** Whether `partial` extends to at least one legal full row (depth-first, first hit wins). */
export function completable(factors: Factor[], partial: Combo, constraint: Constraint): boolean {
  if (!constraint(partial)) return false;
  const open = factors.filter((f) => partial[f.name] === undefined);
  const walk = (i: number, row: Combo): boolean => {
    if (i === open.length) return true;
    for (const v of open[i].values) {
      const next = { ...row, [open[i].name]: v };
      if (constraint(next) && walk(i + 1, next)) return true;
    }
    return false;
  };
  return walk(0, partial);
}

/** Every pair of values across two different factors that some legal full row contains.
 *  (A pair the constraint cannot judge on its own — e.g. two method-dependent factors with
 *  no method yet — is still rejected when no completion exists.) */
export function feasiblePairs(factors: Factor[], constraint: Constraint = () => true): Pair[] {
  const out: Pair[] = [];
  for (let i = 0; i < factors.length; i += 1) {
    for (let j = i + 1; j < factors.length; j += 1) {
      for (const va of factors[i].values) {
        for (const vb of factors[j].values) {
          const partial: Combo = { [factors[i].name]: va, [factors[j].name]: vb };
          if (completable(factors, partial, constraint)) out.push({ a: [factors[i].name, va], b: [factors[j].name, vb] });
        }
      }
    }
  }
  return out;
}

/** Size of the legal cartesian product (depth-first with constraint pruning). */
export function fullProductSize(factors: Factor[], constraint: Constraint = () => true): number {
  let count = 0;
  const walk = (i: number, partial: Combo) => {
    if (i === factors.length) {
      count += 1;
      return;
    }
    for (const v of factors[i].values) {
      const next = { ...partial, [factors[i].name]: v };
      if (constraint(next)) walk(i + 1, next);
    }
  };
  walk(0, {});
  return count;
}

/** Pairs of `combo` not yet in `covered`; returns the keys it would add. */
function newPairKeys(combo: Combo, names: string[], covered: Set<string>): string[] {
  const keys: string[] = [];
  for (let i = 0; i < names.length; i += 1) {
    for (let j = i + 1; j < names.length; j += 1) {
      const key = pairKey(names[i], combo[names[i]], names[j], combo[names[j]]);
      if (!covered.has(key)) keys.push(key);
    }
  }
  return keys;
}

export function allPairs(factors: Factor[], opts: PairwiseOptions = {}): PairwiseResult {
  const constraint = opts.constraint ?? (() => true);
  const random = rng(opts.seed ?? 1);
  const candidates = opts.candidates ?? 24;
  const names = factors.map((f) => f.name);
  const byName = new Map(factors.map((f) => [f.name, f.values]));
  const pairs = feasiblePairs(factors, constraint);
  const uncovered = new Set(pairs.map((p) => pairKey(p.a[0], p.a[1], p.b[0], p.b[1])));
  const pairCount = uncovered.size;
  const combos: Combo[] = [];

  const shuffled = <T>(xs: readonly T[]): T[] => {
    const a = [...xs];
    for (let i = a.length - 1; i > 0; i -= 1) {
      const j = Math.floor(random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  };

  // Build one candidate (AETG): seed it with a still-uncovered pair, then fill the other
  // factors in random order, each with the value covering the most uncovered pairs among
  // the values that still complete into a legal row. Every uncovered pair is completable
  // (feasiblePairs), so a candidate never dead-ends and every round makes progress.
  const candidate = (seedPair: Pair): Combo => {
    const partial: Combo = { [seedPair.a[0]]: seedPair.a[1], [seedPair.b[0]]: seedPair.b[1] };
    for (const name of shuffled(names)) {
      if (partial[name] !== undefined) continue;
      let best: string[] = [];
      let bestGain = -1;
      for (const v of shuffled(byName.get(name) ?? [])) {
        const next = { ...partial, [name]: v };
        if (!completable(factors, next, constraint)) continue;
        let gain = 0;
        for (const other of Object.keys(partial)) {
          if (uncovered.has(pairKey(name, v, other, partial[other]))) gain += 1;
        }
        if (gain > bestGain) {
          bestGain = gain;
          best = [v];
        } else if (gain === bestGain) best.push(v);
      }
      partial[name] = best[Math.floor(random() * best.length)];
    }
    return partial;
  };

  const pairByKey = new Map(pairs.map((p) => [pairKey(p.a[0], p.a[1], p.b[0], p.b[1]), p]));
  while (uncovered.size > 0) {
    let bestCombo: Combo | null = null;
    let bestKeys: string[] = [];
    const remaining = [...uncovered];
    for (let c = 0; c < candidates; c += 1) {
      const seed = pairByKey.get(remaining[Math.floor(random() * remaining.length)]);
      if (!seed) break;
      const combo = candidate(seed);
      const fresh = newPairKeys(combo, names, new Set()).filter((k) => uncovered.has(k));
      if (fresh.length > bestKeys.length) {
        bestCombo = combo;
        bestKeys = fresh;
      }
    }
    if (!bestCombo || bestKeys.length === 0) break; // cannot happen for completable pairs; guards a bad constraint
    combos.push(bestCombo);
    for (const k of bestKeys) uncovered.delete(k);
  }

  const infeasiblePairs = pairs.filter((p) => uncovered.has(pairKey(p.a[0], p.a[1], p.b[0], p.b[1])));
  return { combos, infeasiblePairs, pairCount, fullProductSize: fullProductSize(factors, constraint) };
}

/** Pairs of `factors` that `combos` leave uncovered (constraint-feasible ones only). */
export function uncoveredPairs(factors: Factor[], combos: Combo[], constraint: Constraint = () => true): Pair[] {
  const covered = new Set<string>();
  const names = factors.map((f) => f.name);
  for (const combo of combos) for (const k of newPairKeys(combo, names, new Set())) covered.add(k);
  return feasiblePairs(factors, constraint).filter((p) => !covered.has(pairKey(p.a[0], p.a[1], p.b[0], p.b[1])));
}

/** Stable one-line label of a combination (for logs, screenshot names, register rows). */
export function comboLabel(combo: Combo): string {
  return Object.keys(combo)
    .sort()
    .map((k) => `${k}=${combo[k]}`)
    .join(" ");
}
