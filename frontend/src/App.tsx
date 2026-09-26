import { lazy, type ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation, useParams } from "react-router-dom";

import { ToastProvider } from "./components";
import { useUiVersion } from "./lib/ui-version";
import { RouteChunk } from "./layout/RouteChunk";
import { Shell } from "./layout/Shell";
import { NotFound } from "./pages/NotFound";
import { Overview } from "./pages/Overview";
import { WorkspaceProvider } from "./workspace/WorkspaceProvider";
import { classicAgentNewToV2 } from "./v2/pages/agents/classicUrl";
import { classicAssistantToV2 } from "./v2/pages/assistant/classicUrl";
import { classicRegistryToV2 } from "./v2/pages/registry/classicUrl";
import { classicKnowledgeBasesToV2 } from "./v2/pages/knowledge/classicUrl";
import { classicSkillLabToV2 } from "./v2/pages/skilllab/classicUrl";
import { classicChatToV2 } from "./v2/pages/chat/classicUrl";
import { classicObservabilityToV2 } from "./v2/pages/observability/classicUrl";
import { classicMemoryToV2 } from "./v2/pages/memory/classicUrl";
import { classicGovernanceToV2 } from "./v2/pages/governance/classicUrl";

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

// Console V2 (light enterprise-SaaS experience): native V2 pages live under /v2;
// every classic route keeps its URL and, once the operator chose V2, renders
// inside the V2 shell instead of the classic one (see ConsoleShell).
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
const V2NotFound = lazy(() =>
  import("./v2/pages/Home").then((m) => ({ default: m.V2NotFound })),
);

/**
 * Classic module routes whose page has a native V2 twin: once the operator chose
 * V2, the classic URL (links from other modules, bookmarks, hand-overs) is mapped
 * onto the native page; the classic console keeps rendering the classic page.
 */
function V2OrClassic({ classic, toV2 }: { classic: ReactNode; toV2: (search: string) => string }) {
  const { search } = useLocation();
  if (useUiVersion() === "v2") return <Navigate to={toV2(search)} replace />;
  return <>{classic}</>;
}

/** The index route honours the operator's remembered console choice. */
function IndexRoute() {
  return useUiVersion() === "v2" ? <Navigate to="/v2" replace /> : <Overview />;
}

/**
 * Agent list and detail have a native V2 page: in V2 the classic URLs (links from
 * other modules, the wizard's hand-over after a deploy) land there. Creating,
 * editing and importing stay on the classic flows, inside the V2 shell.
 */
function AgentsRoute({ mode }: { mode: "list" | "detail" }) {
  const { agentId } = useParams();
  if (useUiVersion() === "v2") {
    const to = mode === "detail" && agentId ? `/v2/agents?view=detail&id=${agentId}` : "/v2/agents";
    return <Navigate to={to} replace />;
  }
  return <CreateAgent mode={mode} />;
}

/**
 * `/agents/new` with a creation intent (method / gateway / skill prefill) opens
 * the native V2 wizard in V2; the bare URL keeps the classic system-presets page.
 */
function AgentsNewRoute() {
  const { search } = useLocation();
  const target = useUiVersion() === "v2" ? classicAgentNewToV2(search) : null;
  return target ? <Navigate to={target} replace /> : <CreateAgent mode="new" />;
}

/**
 * In V2 the classic evaluation page's new-run, experiment and online sub-pages have
 * native twins: their URLs (hand-offs from runs, agents, bookmarks) are mapped onto them.
 * Every other `/evaluation` view stays classic inside the V2 shell.
 */
function v2EvaluationTarget(search: string): string | null {
  const params = new URLSearchParams(search);
  const view = params.get("view");
  const next = new URLSearchParams();
  if (view === "online") {
    const oe = params.get("oe");
    if (oe === "new") next.set("view", "new");
    else if (oe) {
      next.set("view", "detail");
      next.set("id", oe);
    }
    const q = next.toString();
    return `/v2/eval/online${q ? `?${q}` : ""}`;
  }
  // the new-run form: hand-offs prefill agent / dataset / evaluators. A run
  // started for an experiment baseline (`return=experiment`) stays classic,
  // since only the classic form returns to the experiment afterwards.
  if (view === "new" && !params.get("return")) {
    next.set("view", "new");
    for (const key of ["agent", "dataset", "evaluators"]) {
      const value = params.get(key);
      if (value) next.set(key, value);
    }
    return `/v2/eval/tasks?${next.toString()}`;
  }
  if (view === "experiment") {
    if (params.get("mode") === "canary") {
      next.set("mode", "canary");
      for (const key of ["canary", "champion", "sourceExp"]) {
        const value = params.get(key);
        if (value) next.set(key, value);
      }
    } else {
      const exp = params.get("exp");
      if (exp === "new") {
        next.set("view", "new");
        for (const key of ["agent", "lookback", "baselineRun", "sourceRun"]) {
          const value = params.get(key);
          if (value) next.set(key, value);
        }
      } else if (exp) {
        next.set("view", "detail");
        next.set("id", exp);
      }
    }
    const q = next.toString();
    return `/v2/eval/experiments${q ? `?${q}` : ""}`;
  }
  return null;
}

function EvaluationRoute() {
  const { search } = useLocation();
  const target = useUiVersion() === "v2" ? v2EvaluationTarget(search) : null;
  return target ? <Navigate to={target} replace /> : <Evaluation />;
}

/**
 * Chrome for the classic routes: the classic shell, or the V2 shell when the
 * operator chose V2 — so links between modules (`/agents/…`, `/chat?agent=…`)
 * work unchanged in both consoles and switching keeps the current page.
 */
function ConsoleShell() {
  return useUiVersion() === "v2" ? (
    <RouteChunk>
      <V2Shell classic />
    </RouteChunk>
  ) : (
    <Shell />
  );
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
              <Route path="*" element={<V2NotFound />} />
            </Route>
            <Route element={<ConsoleShell />}>
            <Route index element={<IndexRoute />} />
            <Route path="agents" element={<AgentsRoute mode="list" />} />
            <Route path="agents/new" element={<AgentsNewRoute />} />
            <Route path="agents/import" element={<CreateAgent mode="import" />} />
            <Route path="agents/:agentId" element={<AgentsRoute mode="detail" />} />
            <Route path="agents/:agentId/edit" element={<CreateAgent mode="edit" />} />
            <Route path="create" element={<LegacyCreateRedirect />} />
            <Route path="create/studio" element={<CreateAgentStudio />} />
              <Route
                path="create/assistant"
                element={<V2OrClassic classic={<CreateAgentAssistant />} toV2={classicAssistantToV2} />}
              />
              <Route
                path="registry"
                element={<V2OrClassic classic={<Registry />} toV2={classicRegistryToV2} />}
              />
              <Route
                path="knowledge-bases"
                element={<V2OrClassic classic={<KnowledgeBases />} toV2={classicKnowledgeBasesToV2} />}
              />
              <Route
                path="memory"
                element={<V2OrClassic classic={<Memory />} toV2={classicMemoryToV2} />}
              />
              <Route
                path="chat"
                element={<V2OrClassic classic={<Chat />} toV2={classicChatToV2} />}
              />
              <Route
                path="observability"
                element={<V2OrClassic classic={<Observability />} toV2={classicObservabilityToV2} />}
              />
              <Route path="evaluation" element={<EvaluationRoute />} />
              <Route
                path="skill-lab"
                element={<V2OrClassic classic={<SkillLab />} toV2={classicSkillLabToV2} />}
              />
              <Route
                path="governance"
                element={<V2OrClassic classic={<Governance />} toV2={classicGovernanceToV2} />}
              />
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
