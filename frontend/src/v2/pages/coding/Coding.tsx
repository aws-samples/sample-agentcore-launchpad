import { useSearchParams } from "react-router-dom";

import "./coding.css";
import { ProjectIde } from "./ProjectIde";
import { ProjectList } from "./ProjectList";

/**
 * Coding Agent — a member's coding projects on the workspace's platform coding
 * runtime (codex app-server in an AgentCore microVM). The list is the landing page;
 * `?view=project&id=<project_id>` opens a project's IDE (file tree, editor and diffs,
 * the conversation with the coding agent, deploy as an Agent).
 */
export function V2Coding() {
  const [params] = useSearchParams();
  const id = params.get("id");
  if (params.get("view") === "project" && id) return <ProjectIde key={id} id={id} />;
  return <ProjectList />;
}
