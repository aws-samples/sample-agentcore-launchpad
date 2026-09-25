import { lazy } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";

import { ToastProvider } from "./components";
import { getUiVersion } from "./lib/ui-version";
import { RouteChunk } from "./layout/RouteChunk";
import { Shell } from "./layout/Shell";
import { NotFound } from "./pages/NotFound";
import { Overview } from "./pages/Overview";
import { WorkspaceProvider } from "./workspace/WorkspaceProvider";

// Every module but the index route and the catch-all is fetched on navigation.
// The pages carry the console's weight (the Studio canvas alone pulls
// @xyflow/react + the monaco loader, Chat and the session detail pull the
// markdown/highlight stack), so a static route table shipped all thirteen to
// every visitor in the entry chunk. The pages export their component by name,
// hence the `.then()` shim `React.lazy` needs — same shape as the live-view
// boundary in `pages/governance/ToolsView.tsx`.
//
// `Overview` stays eager: it is the index route, so a chunk for it would add a
// round trip to the very first paint and nothing else would use it. `NotFound`
// stays eager too — it is the fallback for a typo'd URL and a few hundred bytes.
const Chat = lazy(() => import("./pages/Chat").then((m) => ({ default: m.Chat })));
const CreateAgent = lazy(() =>
  import("./pages/CreateAgent").then((m) => ({ default: m.CreateAgent })),
);

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
const CreateAgentStudio = lazy(() =>
  import("./pages/CreateAgentStudio").then((m) => ({ default: m.CreateAgentStudio })),
);
const CreateAgentAssistant = lazy(() =>
  import("./pages/CreateAgentAssistant").then((m) => ({ default: m.CreateAgentAssistant })),
);
const Evaluation = lazy(() =>
  import("./pages/Evaluation").then((m) => ({ default: m.Evaluation })),
);
const Governance = lazy(() =>
  import("./pages/Governance").then((m) => ({ default: m.Governance })),
);
const KnowledgeBases = lazy(() =>
  import("./pages/KnowledgeBases").then((m) => ({ default: m.KnowledgeBases })),
);
const Memory = lazy(() => import("./pages/Memory").then((m) => ({ default: m.Memory })));
const Observability = lazy(() =>
  import("./pages/Observability").then((m) => ({ default: m.Observability })),
);
const Registry = lazy(() => import("./pages/Registry").then((m) => ({ default: m.Registry })));
const SkillLab = lazy(() => import("./pages/SkillLab").then((m) => ({ default: m.SkillLab })));
const Users = lazy(() => import("./pages/Users").then((m) => ({ default: m.Users })));
const Announcements = lazy(() =>
  import("./pages/Announcements").then((m) => ({ default: m.Announcements })),
);
const Videos = lazy(() => import("./pages/Videos").then((m) => ({ default: m.Videos })));
const Workspaces = lazy(() =>
  import("./pages/Workspaces").then((m) => ({ default: m.Workspaces })),
);

// Console V2 (light enterprise-SaaS experience) lives under /v2 with its own
// shell; the classic console keeps every existing route. Both ship side by side.
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
const V2NotFound = lazy(() =>
  import("./v2/pages/Home").then((m) => ({ default: m.V2NotFound })),
);

/** The index route honours the operator's remembered console choice. */
function IndexRoute() {
  return getUiVersion() === "v2" ? <Navigate to="/v2" replace /> : <Overview />;
}

export default function App() {
  return (
    <BrowserRouter>
      <ToastProvider>
        <WorkspaceProvider>
          <Routes>
            <Route
              path="v2"
              element={
                <RouteChunk>
                  <V2Shell />
                </RouteChunk>
              }
            >
              <Route index element={<V2Home />} />
              <Route path="eval/data" element={<V2DataCenter />} />
              <Route path="eval/tasks" element={<V2Tasks />} />
              <Route path="eval/insights" element={<V2Insights />} />
              <Route path="eval/evaluators" element={<V2Evaluators />} />
              <Route path="*" element={<V2NotFound />} />
            </Route>
            <Route element={<Shell />}>
            <Route index element={<IndexRoute />} />
            <Route path="agents" element={<CreateAgent mode="list" />} />
            <Route path="agents/new" element={<CreateAgent mode="new" />} />
            <Route path="agents/import" element={<CreateAgent mode="import" />} />
            <Route path="agents/:agentId" element={<CreateAgent mode="detail" />} />
            <Route path="agents/:agentId/edit" element={<CreateAgent mode="edit" />} />
            <Route path="create" element={<LegacyCreateRedirect />} />
            <Route path="create/studio" element={<CreateAgentStudio />} />
              <Route path="create/assistant" element={<CreateAgentAssistant />} />
              <Route path="registry" element={<Registry />} />
              <Route path="knowledge-bases" element={<KnowledgeBases />} />
              <Route path="memory" element={<Memory />} />
              <Route path="chat" element={<Chat />} />
              <Route path="observability" element={<Observability />} />
              <Route path="evaluation" element={<Evaluation />} />
              <Route path="skill-lab" element={<SkillLab />} />
              <Route path="governance" element={<Governance />} />
              <Route path="users" element={<Users />} />
              <Route path="announcements" element={<Announcements />} />
              <Route path="videos" element={<Videos />} />
              <Route path="workspaces" element={<Workspaces />} />
              {/* Catch-all stays INSIDE the Shell group so an unknown URL keeps
                  the sidebar/topbar/footer instead of a bare background grid. */}
              <Route path="*" element={<NotFound />} />
            </Route>
          </Routes>
        </WorkspaceProvider>
      </ToastProvider>
    </BrowserRouter>
  );
}
