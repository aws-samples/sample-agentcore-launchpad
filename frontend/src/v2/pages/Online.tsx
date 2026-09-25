import { useSearchParams } from "react-router-dom";

import { OnlineDetail } from "./online/OnlineDetail";
import { OnlineEditor } from "./online/OnlineEditor";
import { OnlineList } from "./online/OnlineList";

/**
 * 在线评估 — every online evaluation config of the workspace (agent-owned,
 * experiment arms, external), scores and insights modes. Sub-pages are `?view=`
 * states: `new`, `edit&id=` (agent-owned only) and `detail&id=`.
 */
export function V2Online() {
  const [params] = useSearchParams();
  const view = params.get("view");
  const id = params.get("id");
  if (view === "new") return <OnlineEditor key="new" id={null} />;
  if (view === "edit" && id) return <OnlineEditor key={`edit:${id}`} id={id} />;
  if (view === "detail" && id) return <OnlineDetail key={id} id={id} />;
  return <OnlineList />;
}
