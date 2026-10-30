import { lazy, useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation, useParams } from "react-router-dom";

import { ToastProvider } from "./components";
import { v2EvaluationTarget } from "./lib/evaluation-url";
import { RouteChunk } from "./layout/RouteChunk";
import { WorkspaceProvider } from "./workspace/WorkspaceProvider";
import { api } from "./lib/api";
import { classicAgentNewToV2, v2EditPath } from "./v2/pages/agents/classicUrl";
import { classicAssistantToV2 } from "./v2/pages/assistant/classicUrl";
import { classicUsersToV2 } from "./v2/pages/users/classicUrl";
import { classicWorkspacesToV2 } from "./v2/pages/workspaces/classicUrl";
import { classicAnnouncementsToV2 } from "./v2/pages/announcements/classicUrl";
import { classicVideosToV2, legacyVideoManagementToV2 } from "./v2/pages/videos/classicUrl";
import { classicRegistryToV2 } from "./v2/pages/registry/classicUrl";
import { classicSkillLabToV2 } from "./v2/pages/skilllab/classicUrl";
import { classicKnowledgeBasesToV2 } from "./v2/pages/knowledge/classicUrl";
import { classicChatToV2 } from "./v2/pages/chat/classicUrl";
import { classicObservabilityToV2 } from "./v2/pages/observability/classicUrl";
import { classicMemoryToV2 } from "./v2/pages/memory/classicUrl";
import { classicGovernanceToV2 } from "./v2/pages/governance/classicUrl";
import { classicModelProvidersToV2 } from "./v2/pages/modelProviders/classicUrl";
import { legacyMyConnectionsToV2 } from "./v2/pages/connections/legacyUrl";
import { V2NotFound } from "./v2/pages/NotFound";

// Every page module is fetched on navigation: the Studio canvas alone pulls
// @xyflow/react + the monaco loader, Chat and the session detail pull the
// markdown/highlight stack, so a static route table would ship all of them to
// every visitor in the entry chunk. The pages export their component by name,
// hence the `.then()` shim `React.lazy` needs. `V2NotFound` stays eager — it is
// the fallback for a typo'd URL and a few hundred bytes.

// The agent flows that have no native V2 page yet render on the V2 light theme
// (v2-classic.css) inside the V2 shell: the system-presets surface (bare
// `/agents/new`), the Runtime / Harness discovery (`/agents/import`), a system
// preset's configure flow (`/agents/:id/edit`) and the Studio canvas.
const CreateAgent = lazy(() =>
  import("./pages/CreateAgent").then((m) => ({ default: m.CreateAgent })),
);
const CreateAgentStudio = lazy(() =>
  import("./pages/CreateAgentStudio").then((m) => ({ default: m.CreateAgentStudio })),
);

const V2Shell = lazy(() => import("./v2/V2Shell").then((m) => ({ default: m.V2Shell })));
const V2Home = lazy(() => import("./v2/pages/Home").then((m) => ({ default: m.V2Home })));
const V2DataCenter = lazy(() =>
  import("./v2/pages/DataCenter").then((m) => ({ default: m.V2DataCenter })),
);
const V2Tasks = lazy(() => import("./v2/pages/Tasks").then((m) => ({ default: m.V2Tasks })));
const V2Insights = lazy(() =>
  import("./v2/pages/Insights").then((m) => ({ default: m.V2Insights })),
);
const V2Evaluators = lazy(() =>
  import("./v2/pages/Evaluators").then((m) => ({ default: m.V2Evaluators })),
);
const V2Agents = lazy(() => import("./v2/pages/Agents").then((m) => ({ default: m.V2Agents })));
const V2Online = lazy(() => import("./v2/pages/Online").then((m) => ({ default: m.V2Online })));
const V2Experiments = lazy(() =>
  import("./v2/pages/Experiments").then((m) => ({ default: m.V2Experiments })),
);
const V2Assistant = lazy(() => import("./v2/pages/Assistant").then((m) => ({ default: m.V2Assistant })));
const V2Registry = lazy(() => import("./v2/pages/Registry").then((m) => ({ default: m.V2Registry })));
const V2KnowledgeBases = lazy(() => import("./v2/pages/KnowledgeBases").then((m) => ({ default: m.V2KnowledgeBases })));
const V2SkillLab = lazy(() => import("./v2/pages/SkillLab").then((m) => ({ default: m.V2SkillLab })));
const V2Chat = lazy(() => import("./v2/pages/Chat").then((m) => ({ default: m.V2Chat })));
const V2Observability = lazy(() => import("./v2/pages/Observability").then((m) => ({ default: m.V2Observability })));
const V2Memory = lazy(() => import("./v2/pages/Memory").then((m) => ({ default: m.V2Memory })));
const V2Governance = lazy(() => import("./v2/pages/Governance").then((m) => ({ default: m.V2Governance })));
const V2Policy = lazy(() => import("./v2/pages/Policy").then((m) => ({ default: m.V2Policy })));
const V2Users = lazy(() => import("./v2/pages/Users").then((m) => ({ default: m.V2Users })));
const V2Workspaces = lazy(() => import("./v2/pages/Workspaces").then((m) => ({ default: m.V2Workspaces })));
const V2Connections = lazy(() => import("./v2/pages/Connections").then((m) => ({ default: m.V2Connections })));
// as_user consent return page: signed in (the binding leg needs the caller) but
// outside the shell — it is a tab the IdP redirect lands in, closed after.
const AuthReturn = lazy(() => import("./v2/pages/AuthReturn").then((m) => ({ default: m.AuthReturn })));
const V2Announcements = lazy(() => import("./v2/pages/Announcements").then((m) => ({ default: m.V2Announcements })));
const V2Videos = lazy(() => import("./v2/pages/Videos").then((m) => ({ default: m.V2Videos })));
// D21: admin-only model registry
const V2ModelProviders = lazy(() =>
  import("./v2/pages/ModelProviders").then((m) => ({ default: m.V2ModelProviders })),
);

/**
 * A retired classic module route. The URL (links in docs and announcements,
 * bookmarks, hand-overs between modules) is mapped onto the native V2 page by the
 * module's `classicUrl` helper, which carries the deep link (`?view=`, `?agent=`,
 * `?session=`, …) across.
 */
function ClassicRedirect({ toV2 }: { toV2: (search: string) => string }) {
  const { search } = useLocation();
  return <Navigate to={toV2(search)} replace />;
}

/**
 * The pre-2026-09-18 management page lived at `/create` (wizard + list on one
 * route, discovery under `?view=discover`). Old links in docs, bookmarks and
 * assistant texts keep working: the query string rides along so Registry's
 * `?gateway=` / `?skill=` prefill still lands on the wizard.
 */
function LegacyCreateRedirect() {
  const { search } = useLocation();
  const params = new URLSearchParams(search);
  if (params.get("view") === "discover") return <Navigate to="/agents/import" replace />;
  params.delete("view");
  const rest = params.toString();
  return <Navigate to={`/agents/new${rest ? `?${rest}` : ""}`} replace />;
}

/** `/v2/my-connections` became the `?view=mine` tab of Connections (R13, N13-obs5). */
function MyConnectionsRedirect() {
  const { search } = useLocation();
  return <Navigate to={legacyMyConnectionsToV2(search)} replace />;
}

/** `/agents/:id` — the native detail view. */
function AgentDetailRedirect() {
  const { agentId } = useParams();
  return <Navigate to={agentId ? `/v2/agents?view=detail&id=${agentId}` : "/v2/agents"} replace />;
}

/**
 * `/agents/:id/edit` opens the native re-publish wizard. A system preset keeps
 * its preset configure flow and a canvas agent its Studio editor, so the row is
 * read first to pick the target.
 */
function AgentsEditRoute() {
  const { agentId } = useParams();
  const [target, setTarget] = useState<string | "preset" | null>(null);
  useEffect(() => {
    if (!agentId) return;
    let live = true;
    api
      .getAgent(agentId)
      .then((agent) => live && setTarget(agent.system ? "preset" : v2EditPath(agent)))
      // unreadable here: the configure page owns the not-found / error handling
      .catch(() => live && setTarget("preset"));
    return () => {
      live = false;
    };
  }, [agentId]);
  if (target === "preset") return <CreateAgent mode="edit" />;
  return target ? <Navigate to={target} replace /> : null;
}

/**
 * `/agents/new` with a creation intent (method / gateway / skill prefill) opens
 * the native V2 wizard; the bare URL is the system-presets page.
 */
function AgentsNewRoute() {
  const { search } = useLocation();
  const target = classicAgentNewToV2(search);
  return target ? <Navigate to={target} replace /> : <CreateAgent mode="new" />;
}

/** `/evaluation?view=…` — the matching V2 evaluation page, or the Insights index. */
function EvaluationRedirect() {
  const { search } = useLocation();
  return <Navigate to={v2EvaluationTarget(search) ?? "/v2/eval/insights"} replace />;
}

export default function App() {
  return (
    <BrowserRouter>
      <ToastProvider>
        <WorkspaceProvider>
          <Routes>
            <Route
              path="auth/return"
              element={
                <RouteChunk>
                  <AuthReturn />
                </RouteChunk>
              }
            />
            <Route
              element={
                <RouteChunk>
                  <V2Shell />
                </RouteChunk>
              }
            >
              <Route path="v2">
                <Route index element={<V2Home />} />
                <Route path="agents" element={<V2Agents />} />
                <Route path="eval/data" element={<V2DataCenter />} />
                <Route path="eval/tasks" element={<V2Tasks />} />
                <Route path="eval/insights" element={<V2Insights />} />
                <Route path="eval/evaluators" element={<V2Evaluators />} />
                <Route path="eval/online" element={<V2Online />} />
                <Route path="eval/experiments" element={<V2Experiments />} />
                <Route path="assistant" element={<V2Assistant />} />
                <Route path="registry" element={<V2Registry />} />
                <Route path="knowledge-bases" element={<V2KnowledgeBases />} />
                <Route path="skill-lab" element={<V2SkillLab />} />
                <Route path="chat" element={<V2Chat />} />
                <Route path="observability" element={<V2Observability />} />
                <Route path="memory" element={<V2Memory />} />
                <Route path="governance" element={<V2Governance />} />
                <Route path="policy" element={<V2Policy />} />
                <Route path="users" element={<V2Users />} />
                <Route path="workspaces" element={<V2Workspaces />} />
                <Route path="connections" element={<V2Connections />} />
                <Route path="my-connections" element={<MyConnectionsRedirect />} />
                <Route path="announcements" element={<V2Announcements />} />
                <Route path="videos" element={<V2Videos />} />
                {/* folded into Videos → 管理 tab (N15-obs14); the old URL keeps its deep link */}
                <Route path="video-management" element={<ClassicRedirect toV2={legacyVideoManagementToV2} />} />
                <Route path="settings/model-providers" element={<V2ModelProviders />} />
                <Route path="*" element={<V2NotFound />} />
              </Route>
              {/* Retired classic routes: each lands on its V2 counterpart with the
                  deep link preserved; an unknown path renders the console's NotFound. */}
              <Route index element={<Navigate to="/v2" replace />} />
              <Route path="agents" element={<Navigate to="/v2/agents" replace />} />
              <Route path="agents/:agentId" element={<AgentDetailRedirect />} />
              <Route path="create" element={<LegacyCreateRedirect />} />
              <Route path="create/assistant" element={<ClassicRedirect toV2={classicAssistantToV2} />} />
              <Route path="registry" element={<ClassicRedirect toV2={classicRegistryToV2} />} />
              <Route path="knowledge-bases" element={<ClassicRedirect toV2={classicKnowledgeBasesToV2} />} />
              <Route path="skill-lab" element={<ClassicRedirect toV2={classicSkillLabToV2} />} />
              <Route path="memory" element={<ClassicRedirect toV2={classicMemoryToV2} />} />
              <Route path="chat" element={<ClassicRedirect toV2={classicChatToV2} />} />
              <Route path="observability" element={<ClassicRedirect toV2={classicObservabilityToV2} />} />
              <Route path="evaluation" element={<EvaluationRedirect />} />
              <Route path="governance" element={<ClassicRedirect toV2={classicGovernanceToV2} />} />
              <Route path="users" element={<ClassicRedirect toV2={classicUsersToV2} />} />
              <Route path="announcements" element={<ClassicRedirect toV2={classicAnnouncementsToV2} />} />
              <Route path="videos" element={<ClassicRedirect toV2={classicVideosToV2} />} />
              <Route path="workspaces" element={<ClassicRedirect toV2={classicWorkspacesToV2} />} />
              <Route path="settings/model-providers" element={<ClassicRedirect toV2={classicModelProvidersToV2} />} />
              <Route path="*" element={<V2NotFound />} />
            </Route>
            {/* The agent flows without a native V2 page: token-styled, inside the V2 shell. */}
            <Route
              element={
                <RouteChunk>
                  <V2Shell classic />
                </RouteChunk>
              }
            >
              <Route path="agents/new" element={<AgentsNewRoute />} />
              <Route path="agents/import" element={<CreateAgent mode="import" />} />
              <Route path="agents/:agentId/edit" element={<AgentsEditRoute />} />
              <Route path="create/studio" element={<CreateAgentStudio />} />
            </Route>
          </Routes>
        </WorkspaceProvider>
      </ToastProvider>
    </BrowserRouter>
  );
}
