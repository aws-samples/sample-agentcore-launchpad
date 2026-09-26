/**
 * Maps a classic `/skill-lab` query string onto the native V2 page, so links from
 * other modules, bookmarks and hand-overs keep landing in the right sub-page.
 *
 * Classic surfaces:  `?view=tasksets|eval|train` (none = task sets),
 * `ts=new|<id>` (task-set editor / detail), `gen=new|<jobId>` (AI generation),
 * `job=new|<jobId>` (+ `record=` preset from the Registry) on eval / train.
 */
export function classicSkillLabToV2(search: string): string {
  const src = new URLSearchParams(search);
  const out = new URLSearchParams();
  const view = src.get("view") ?? "";
  const ts = src.get("ts");
  const gen = src.get("gen");
  const job = src.get("job");
  const record = src.get("record");

  if (view === "eval" || view === "train") {
    out.set("tab", view);
    if (job === "new") {
      out.set("view", "new");
      if (record) out.set("record", record);
    } else if (job) {
      out.set("view", "detail");
      out.set("id", job);
    }
  } else if (gen) {
    out.set("tab", "tasksets");
    if (gen === "new") out.set("view", "gen-new");
    else {
      out.set("view", "gen");
      out.set("id", gen);
    }
  } else {
    out.set("tab", "tasksets");
    if (ts === "new") out.set("view", "new");
    else if (ts) {
      out.set("view", "detail");
      out.set("id", ts);
    }
  }
  return `/v2/skill-lab?${out.toString()}`;
}
