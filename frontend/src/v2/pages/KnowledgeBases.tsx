import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import "./knowledge/knowledge.css";
import { KbCreate } from "./knowledge/KbCreate";
import { KbDetail } from "./knowledge/KbDetail";
import { KbList } from "./knowledge/KbList";

/**
 * 知识库 — managed (fully-managed RAG) knowledge bases of the workspace. Sub-pages
 * are `?view=` states: `new` (create) and `detail&id=<kb_id>` (`&tab=retrieve` opens
 * the retrieve playground). A detail link whose id no longer resolves lands back
 * on the list with a notice naming it.
 */
export function V2KnowledgeBases() {
  const [params, setParams] = useSearchParams();
  const view = params.get("view");
  const id = params.get("id");
  const [staleId, setStaleId] = useState<string | null>(null);

  if (view === "new") return <KbCreate />;
  if (view === "detail" && id) {
    return (
      <KbDetail
        key={id}
        id={id}
        onGone={(gone) => {
          setStaleId(gone);
          setParams({}, { replace: true });
        }}
      />
    );
  }
  // `?view=detail` without an id: say so rather than silently showing the list
  const missing = view === "detail" && !id ? "" : null;
  return (
    <KbList
      staleId={staleId ?? missing}
      onDismissStale={() => {
        setStaleId(null);
        if (view) setParams({}, { replace: true });
      }}
    />
  );
}
