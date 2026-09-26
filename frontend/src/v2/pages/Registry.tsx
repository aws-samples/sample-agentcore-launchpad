import { useSearchParams } from "react-router-dom";

import { A2ADemo } from "./registry/A2ADemo";
import { RecordDetail } from "./registry/RecordDetail";
import { RecordEdit } from "./registry/RecordEdit";
import { DiscoverableList, RecordList } from "./registry/RecordList";
import { RecordRegister } from "./registry/RecordRegister";
import "./registry/registry.css";

/**
 * 注册中心 — AgentCore Registry records (A2A agents, MCP tools, agent skills).
 * Sub-pages are `?view=` states of this route: detail / edit (`&id=`), register
 * (`&type=`), discoverable (consumer view) and a2a-demo.
 */
export function V2Registry() {
  const [params] = useSearchParams();
  const view = params.get("view");
  const id = params.get("id");
  if (view === "detail" && id) return <RecordDetail key={id} id={id} />;
  if (view === "edit" && id) return <RecordEdit key={id} id={id} />;
  if (view === "register") return <RecordRegister key={params.get("type") ?? ""} />;
  if (view === "a2a-demo") return <A2ADemo />;
  if (view === "discoverable") return <DiscoverableList />;
  return <RecordList />;
}
