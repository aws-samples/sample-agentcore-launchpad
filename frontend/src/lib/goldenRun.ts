/**
 * A run over a golden-set split is scored against its criteria lineage's newest
 * published version (`evaluation/routers._golden_lineage`), so the task wizards
 * preselect exactly the evaluators that version's criteria name.
 */
import { dlcApi } from "./api";
import { criteriaEvaluatorIds, latestPublishedVersion } from "./dlc";

export interface GoldenRunCriteria {
  name: string;
  /** null: the lineage has no published version — the run is not scored against criteria */
  version: number | null;
  evaluatorIds: string[];
}

/** What a dataset row needs for this: the list's golden fields. */
export interface GoldenSplitRef {
  role?: string | null;
  split?: string | null;
  criteria_lineage_id?: string | null;
}

export const isGoldenSplit = (d: GoldenSplitRef | null | undefined): d is GoldenSplitRef & { criteria_lineage_id: string } =>
  !!d && d.role === "golden" && !!d.split && !!d.criteria_lineage_id;

export async function goldenRunCriteria(lineageId: string): Promise<GoldenRunCriteria> {
  const newest = await dlcApi.getSet(lineageId);
  const version = latestPublishedVersion(newest.versions);
  if (version === null) return { name: newest.set.name, version: null, evaluatorIds: [] };
  const published = version === newest.set.version ? newest : await dlcApi.getSet(lineageId, version);
  return { name: published.set.name, version, evaluatorIds: criteriaEvaluatorIds(published.criteria) };
}
