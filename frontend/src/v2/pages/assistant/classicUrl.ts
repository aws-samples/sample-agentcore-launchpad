import type { AssistantEvalResource } from "../../../lib/api";
import { asRecord } from "../../../lib/assistant";

/**
 * Maps a classic `/create/assistant` query string onto the native V2 page, so links from
 * other modules, bookmarks and hand-overs keep landing in the right sub-page: the classic
 * `?conversation=<id>` deep link becomes `?view=detail&id=<id>`; anything else lands on
 * the conversation list.
 */
export function classicAssistantToV2(search: string): string {
  const conversation = new URLSearchParams(search).get("conversation");
  if (!conversation) return "/v2/assistant";
  return `/v2/assistant?${new URLSearchParams({ view: "detail", id: conversation }).toString()}`;
}

/**
 * The V2 page of a dataset / evaluator an evaluation-asset operation created. The
 * server's `link` targets the classic `/evaluation?view=datasets|evaluators` pages
 * (which stay classic in the V2 shell), so the V2 creation outcome builds its own link
 * from the same ids: the local dataset id, or the custom / `Builtin.*` evaluator id.
 * Upstream #283 (38a571b1), ported in cn/r11-d.
 */
export function evalResourceV2Link(res: AssistantEvalResource): string | null {
  const result = asRecord(res.result);
  if (res.kind === "dataset" && result.dataset_id) {
    const params = { tab: "datasets", view: "dataset", id: String(result.dataset_id) };
    return `/v2/eval/data?${new URLSearchParams(params).toString()}`;
  }
  if ((res.kind === "evaluator" || res.kind === "existing") && result.evaluator_id) {
    const params = { view: "detail", id: String(result.evaluator_id) };
    return `/v2/eval/evaluators?${new URLSearchParams(params).toString()}`;
  }
  return null;
}
