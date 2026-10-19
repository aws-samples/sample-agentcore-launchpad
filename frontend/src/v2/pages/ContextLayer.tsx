import { useSearchParams } from "react-router-dom";

import { api } from "../../lib/api";
import { useLoad } from "../hooks";
import "./contextLayer/contextLayer.css";
import { NamespaceView } from "./contextLayer/NamespaceView";
import { Overview } from "./contextLayer/Overview";
import { Playground } from "./contextLayer/Playground";
import { contextView, type ContextView, viewSearch } from "./contextLayer/views";

/**
 * 上下文层 (Context layer) — the workspace's connected COA (Context Ontology
 * Accelerator) deployment, read as the signed-in member so COA's grants decide
 * what they see. Sub-pages are `?view=` states (`contextLayer/views.ts`): the
 * overview (deployment, my authorization, namespaces), `namespace&id=` (sources,
 * metrics and ontology tabs, deep links into the COA console, attach to an agent)
 * and `playground&ns=` (streamed questions to COA). Authoring stays in COA.
 *
 * The status is read once here and shared by every view, so moving between them
 * does not re-run its live AWS probes.
 */
export function V2ContextLayer() {
  const [params, setParams] = useSearchParams();
  const view = contextView(params);
  const status = useLoad(() => api.getContextLayer(), "context-layer");
  const go = (next: ContextView, replace = false) => setParams(viewSearch(next), { replace });

  if (view.view === "namespace") {
    return <NamespaceView key={view.id} id={view.id} tab={view.tab} status={status} go={go} />;
  }
  if (view.view === "playground") {
    // one conversation per namespace: switching it starts a new one
    return <Playground key={view.ns ?? ""} ns={view.ns} status={status} go={go} />;
  }
  return <Overview status={status} go={go} />;
}
