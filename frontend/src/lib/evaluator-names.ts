// N17-obs429: the names of the workspace's custom evaluators (custom judges, derived and
// code evaluators), so `evaluatorLabel` shows "hr_policy_citation" rather than the raw id
// "cust_f623957dd060". A plain store: `lib/scale-label-catalog.ts` fills it from
// `GET /api/eval/evaluators`; `v2/useEvaluatorNames.ts` re-renders a view once it is filled.
// An id the catalogue has no row for (a deleted evaluator) keeps its id.
let names: Record<string, string> = {};
let version = 0;
const listeners = new Set<() => void>();

export function setCustomEvaluatorNames(next: Record<string, string>): void {
  names = { ...next };
  version += 1;
  listeners.forEach((listener) => listener());
}

export function customEvaluatorName(id: string): string | undefined {
  return names[id];
}

export function subscribeEvaluatorNames(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function evaluatorNamesVersion(): number {
  return version;
}
