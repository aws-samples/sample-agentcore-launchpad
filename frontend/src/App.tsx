import { lazy, type ReactNode, useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation, useParams } from "react-router-dom";

import { ToastProvider } from "./components";
import { useUiVersion } from "./lib/ui-version";
import { RouteChunk } from "./layout/RouteChunk";
import { Shell } from "./layout/Shell";
import { NotFound } from "./pages/NotFound";
import { Overview } from "./pages/Overview";
import { ProjectProvider } from "./workspace/ProjectProvider";
import { WorkspaceProvider } from "./workspace/WorkspaceProvider";
import { api } from "./lib/api";
import { classicAgentNewToV2, v2EditPath } from "./v2/pages/agents/classicUrl";
import { classicAssistantToV2 } from "./v2/pages/assistant/classicUrl";
import { classicUsersToV2 } from "./v2/pages/users/classicUrl";
import { classicWorkspacesToV2 } from "./v2/pages/workspaces/classicUrl";
import { classicAnnouncementsToV2 } from "./v2/pages/announcements/classicUrl";
import { classicVideosToV2 } from "./v2/pages/videos/classicUrl";
import { classicRegistryToV2 } from "./v2/pages/registry/classicUrl";
import { classicKnowledgeBasesToV2 } from "./v2/pages/knowledge/classicUrl";
import { classicSkillLabToV2 } from "./v2/pages/skilllab/classicUrl";
import { classicChatToV2 } from "./v2/pages/chat/classicUrl";
import { classicObservabilityToV2 } from "./v2/pages/observability/classicUrl";
import { classicMemoryToV2 } from "./v2/pages/memory/classicUrl";
import { classicGovernanceToV2 } from "./v2/pages/governance/classicUrl";
import { v3TwinOf } from "./v3/nav";
import { classicOnlineToTasksUrl, onlineToTasksUrl } from "./v2/pages/tasks/urls";

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
// Console V3 — the redesigned console, the default and switchable with V2 (src/v3/)
const V3Shell = lazy(() => import("./v3/Shell").then((m) => ({ default: m.V3Shell })));
const V3Home = lazy(() => import("./v3/pages/Home").then((m) => ({ default: m.V3Home })));
const V3Agents = lazy(() => import("./v3/pages/Agents").then((m) => ({ default: m.V3Agents })));
const V3Chat = lazy(() => import("./v3/pages/Chat").then((m) => ({ default: m.V3Chat })));
const V3Gate = lazy(() => import("./v3/pages/Gate").then((m) => ({ default: m.V3Gate })));
const V3Registry = lazy(() => import("./v3/pages/Registry").then((m) => ({ default: m.V3Registry })));
const V3Releases = lazy(() => import("./v3/pages/Releases").then((m) => ({ default: m.V3Releases })));
const V3Issues = lazy(() => import("./v3/pages/Issues").then((m) => ({ default: m.V3Issues })));
const V3Environments = lazy(() => import("./v3/pages/Environments").then((m) => ({ default: m.V3Environments })));
const V3Observability = lazy(() => import("./v3/pages/Observability").then((m) => ({ default: m.V3Observability })));
const V3Memory = lazy(() => import("./v3/pages/Memory").then((m) => ({ default: m.V3Memory })));
const V3Governance = lazy(() => import("./v3/pages/Governance").then((m) => ({ default: m.V3Governance })));
const V3Connections = lazy(() => import("./v3/pages/Connections").then((m) => ({ default: m.V3Connections })));
const V3Costs = lazy(() => import("./v3/pages/Costs").then((m) => ({ default: m.V3Costs })));
const V3Insights = lazy(() => import("./v3/pages/Insights").then((m) => ({ default: m.V3Insights })));
const V3Intents = lazy(() => import("./v3/pages/Intents").then((m) => ({ default: m.V3Intents })));
const V3Data = lazy(() => import("./v3/pages/Data").then((m) => ({ default: m.V3Data })));
const V3Evaluators = lazy(() => import("./v3/pages/Evaluators").then((m) => ({ default: m.V3Evaluators })));
const V3Tasks = lazy(() => import("./v3/pages/Tasks").then((m) => ({ default: m.V3Tasks })));
const V3Online = lazy(() => import("./v3/pages/Online").then((m) => ({ default: m.V3Online })));
const V3Experiments = lazy(() => import("./v3/pages/Experiments").then((m) => ({ default: m.V3Experiments })));
const V3Standards = lazy(() => import("./v3/pages/Standards").then((m) => ({ default: m.V3Standards })));
const V3SkillLab = lazy(() => import("./v3/pages/SkillLab").then((m) => ({ default: m.V3SkillLab })));
const V3Users = lazy(() => import("./v3/pages/Users").then((m) => ({ default: m.V3Users })));
const V3Workspaces = lazy(() => import("./v3/pages/Workspaces").then((m) => ({ default: m.V3Workspaces })));
const V3Models = lazy(() => import("./v3/pages/Models").then((m) => ({ default: m.V3Models })));
const V3Identity = lazy(() => import("./v3/pages/Identity").then((m) => ({ default: m.V3Identity })));
const V3Fleet = lazy(() => import("./v3/pages/Fleet").then((m) => ({ default: m.V3Fleet })));
const V3Announcements = lazy(() => import("./v3/pages/Announcements").then((m) => ({ default: m.V3Announcements })));
const V3Videos = lazy(() => import("./v3/pages/Videos").then((m) => ({ default: m.V3Videos })));
const V3VideoManagement = lazy(() => import("./v3/pages/VideoManagement").then((m) => ({ default: m.V3VideoManagement })));
const V3Assistant = lazy(() => import("./v3/pages/Assistant").then((m) => ({ default: m.V3Assistant })));
const V3Create = lazy(() => import("./v3/pages/Create").then((m) => ({ default: m.V3Create })));
// Configure → Projects (projects design §6.3) and the §6.4 project-owner entry
const V3Projects = lazy(() => import("./v3/pages/projects/Projects").then((m) => ({ default: m.V3Projects })));
const V3ProjectSettings = lazy(() =>
  import("./v3/pages/projects/Projects").then((m) => ({ default: m.V3ProjectSettings })),
);
// Configure → Administrators / Departments (projects §7.9)
const V3Admins = lazy(() => import("./v3/pages/admins/Admins").then((m) => ({ default: m.V3Admins })));
const V3Departments = lazy(() =>
  import("./v3/pages/departments/Departments").then((m) => ({ default: m.V3Departments })),
);
// Configure → Network profiles (projects §12.12)
const V3NetworkProfiles = lazy(() =>
  import("./v3/pages/network/NetworkProfiles").then((m) => ({ default: m.V3NetworkProfiles })),
);
// Configure → Storage (storage-mounts §10)
const V3Storage = lazy(() => import("./v3/pages/storage/Storage").then((m) => ({ default: m.V3Storage })));
// Configure → Cost tags (cost-tags §1.6)
const V3CostTags = lazy(() => import("./v3/pages/cost-tags/CostTags").then((m) => ({ default: m.V3CostTags })));
// Configure → Single sign-on (sso §2.7)
const V3Sso = lazy(() => import("./v3/pages/sso/Sso").then((m) => ({ default: m.V3Sso })));
// Configure → System update (online-update §2.7)
const V3SystemUpdate = lazy(() =>
  import("./v3/pages/system-update/SystemUpdate").then((m) => ({ default: m.V3SystemUpdate })),
);
// Configure → Menu settings: the rail modules members see (services/nav_settings)
const V3MenuSettings = lazy(() =>
  import("./v3/pages/menu-settings/MenuSettings").then((m) => ({ default: m.V3MenuSettings })),
);
const V3Knowledge = lazy(() => import("./v3/pages/Knowledge").then((m) => ({ default: m.V3Knowledge })));
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
const V2Experiments = lazy(() =>
  import("./v2/pages/Experiments").then((m) => ({ default: m.V2Experiments })),
);
const V2Assistant = lazy(() => import("./v2/pages/Assistant").then((m) => ({ default: m.V2Assistant })));
const V2Registry = lazy(() => import("./v2/pages/Registry").then((m) => ({ default: m.V2Registry })));
const V2KnowledgeBases = lazy(() => import("./v2/pages/KnowledgeBases").then((m) => ({ default: m.V2KnowledgeBases })));
const V2ContextLayer = lazy(() => import("./v2/pages/ContextLayer").then((m) => ({ default: m.V2ContextLayer })));
const V2SkillLab = lazy(() => import("./v2/pages/SkillLab").then((m) => ({ default: m.V2SkillLab })));
const V2Coding = lazy(() => import("./v2/pages/coding/Coding").then((m) => ({ default: m.V2Coding })));
const V2Chat = lazy(() => import("./v2/pages/Chat").then((m) => ({ default: m.V2Chat })));
const V2Observability = lazy(() => import("./v2/pages/Observability").then((m) => ({ default: m.V2Observability })));
const V2Memory = lazy(() => import("./v2/pages/Memory").then((m) => ({ default: m.V2Memory })));
const V2Governance = lazy(() => import("./v2/pages/Governance").then((m) => ({ default: m.V2Governance })));
const V2Users = lazy(() => import("./v2/pages/Users").then((m) => ({ default: m.V2Users })));
const V2Fleet = lazy(() => import("./v2/pages/Fleet").then((m) => ({ default: m.V2Fleet })));
const V2Intents = lazy(() => import("./v2/pages/Intents").then((m) => ({ default: m.V2Intents })));
const V2Issues = lazy(() => import("./v2/pages/Issues").then((m) => ({ default: m.V2Issues })));
const V2Costs = lazy(() => import("./v2/pages/Costs").then((m) => ({ default: m.V2Costs })));
const V2Promotions = lazy(() => import("./v2/pages/Promotions").then((m) => ({ default: m.V2Promotions })));
const V2Standards = lazy(() => import("./v2/pages/Standards").then((m) => ({ default: m.V2Standards })));
const V2Environments = lazy(() => import("./v2/pages/Environments").then((m) => ({ default: m.V2Environments })));
const V2Workspaces = lazy(() => import("./v2/pages/Workspaces").then((m) => ({ default: m.V2Workspaces })));
const V2Connections = lazy(() => import("./v2/pages/Connections").then((m) => ({ default: m.V2Connections })));
const V2MyConnections = lazy(() => import("./v2/pages/MyConnections").then((m) => ({ default: m.V2MyConnections })));
// as_user consent return page: signed in (the binding leg needs the caller) but
// outside both shells — it is a tab the IdP redirect lands in, closed after.
const AuthReturn = lazy(() => import("./v2/pages/AuthReturn").then((m) => ({ default: m.AuthReturn })));
const V2Announcements = lazy(() => import("./v2/pages/Announcements").then((m) => ({ default: m.V2Announcements })));
const V2Videos = lazy(() => import("./v2/pages/Videos").then((m) => ({ default: m.V2Videos })));
const V2VideoManagement = lazy(() =>
  import("./v2/pages/VideoManagement").then((m) => ({ default: m.V2VideoManagement })));
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
  if (useUiVersion() !== "v1") return <Navigate to={toV2(search)} replace />;
  return <>{classic}</>;
}

/** The index route honours the operator's remembered console choice. */
function IndexRoute() {
  const version = useUiVersion();
  if (version === "v3") return <Navigate to="/v3" replace />;
  return version === "v2" ? <Navigate to="/v2" replace /> : <Overview />;
}

/**
 * Agent list and detail have a native V2 page: in V2 the classic URLs (links from
 * other modules, the wizard's hand-over after a deploy) land there. Creating,
 * editing and importing stay on the classic flows, inside the V2 shell.
 */
function AgentsRoute({ mode }: { mode: "list" | "detail" }) {
  const { agentId } = useParams();
  if (useUiVersion() !== "v1") {
    const to = mode === "detail" && agentId ? `/v2/agents?view=detail&id=${agentId}` : "/v2/agents";
    return <Navigate to={to} replace />;
  }
  return <CreateAgent mode={mode} />;
}

/**
 * `/agents/:id/edit` in V2 opens the native re-publish wizard. A system preset keeps
 * its preset configure flow (the classic page, inside the V2 shell) and a canvas
 * agent its Studio editor, so the row is read first to pick the target.
 */
function AgentsEditRoute() {
  const { agentId } = useParams();
  const v2 = useUiVersion() !== "v1";
  const [target, setTarget] = useState<string | "classic" | null>(null);
  useEffect(() => {
    if (!v2 || !agentId) return;
    let live = true;
    api
      .getAgent(agentId)
      .then((agent) => live && setTarget(agent.system ? "classic" : v2EditPath(agent)))
      // unreadable here: the classic page owns the not-found / error handling
      .catch(() => live && setTarget("classic"));
    return () => {
      live = false;
    };
  }, [v2, agentId]);
  if (!v2 || target === "classic") return <CreateAgent mode="edit" />;
  return target ? <Navigate to={target} replace /> : null;
}

/**
 * `/agents/new` with a creation intent (method / gateway / skill prefill) opens
 * the native V2 wizard in V2; the bare URL keeps the classic system-presets page.
 */
function AgentsNewRoute() {
  const { search } = useLocation();
  const target = useUiVersion() !== "v1" ? classicAgentNewToV2(search) : null;
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
  // online configs are 评估任务's 持续评估 tab: straight there, not via `/v2/eval/online`
  if (view === "online") return classicOnlineToTasksUrl(search);
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
      } else {
        next.set("mode", "configuration"); // the V2 page defaults to the canary tab
      }
    }
    const q = next.toString();
    return `/v2/eval/experiments${q ? `?${q}` : ""}`;
  }
  return null;
}

/** The former 在线评估 page (`/v2/eval/online`) is 评估任务's 持续评估 tab now: its
 *  links and bookmarks land on the matching tab or sub-page. */
function V2OnlineRoute() {
  const { search } = useLocation();
  return <Navigate to={onlineToTasksUrl(search)} replace />;
}

function EvaluationRoute() {
  const { search } = useLocation();
  const target = useUiVersion() !== "v1" ? v2EvaluationTarget(search) : null;
  return target ? <Navigate to={target} replace /> : <Evaluation />;
}

/**
 * Chrome for the classic routes: the classic shell, or the V2 / V3 shell when the
 * operator chose one — so links between modules (`/agents/…`, `/chat?agent=…`)
 * work unchanged in both consoles and switching keeps the current page.
 */
function ConsoleShell() {
  const version = useUiVersion();
  if (version === "v1") return <Shell />;
  return <RouteChunk>{version === "v3" ? <V3Shell hosted="classic" /> : <V2Shell classic />}</RouteChunk>;
}

/**
 * Chrome for the native V2 pages: the V2 shell, or — once the operator chose
 * V3 — the V3 shell hosting the same page on the V3 theme, so every link a V2
 * page makes (`/v2/...`) stays inside V3. A page V3 has rebuilt hands over to
 * its V3 page instead (`v3TwinOf`).
 */
function V2Frame() {
  const { pathname, search } = useLocation();
  if (useUiVersion() !== "v3") return <V2Shell />;
  const twin = v3TwinOf(pathname, search);
  if (twin) return <Navigate to={twin} replace />;
  return <V3Shell hosted="v2" />;
}

export default function App() {
  return (
    <BrowserRouter>
      <ToastProvider>
        <WorkspaceProvider>
          <ProjectProvider>
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
                path="v3"
                element={
                  <RouteChunk>
                    <V3Shell />
                  </RouteChunk>
                }
              >
                <Route index element={<V3Home />} />
                <Route path="agents" element={<V3Agents />} />
                <Route path="chat" element={<V3Chat />} />
                <Route path="gate" element={<V3Gate />} />
                <Route path="registry" element={<V3Registry />} />
                <Route path="knowledge" element={<V3Knowledge />} />
                <Route path="create" element={<V3Create />} />
                <Route path="assistant" element={<V3Assistant />} />
                <Route path="insights" element={<V3Insights />} />
                <Route path="intents" element={<V3Intents />} />
                <Route path="data" element={<V3Data />} />
                <Route path="evaluators" element={<V3Evaluators />} />
                <Route path="tasks" element={<V3Tasks />} />
                <Route path="online" element={<V3Online />} />
                <Route path="experiments" element={<V3Experiments />} />
                <Route path="standards" element={<V3Standards />} />
                <Route path="skill-lab" element={<V3SkillLab />} />
                <Route path="users" element={<V3Users />} />
                <Route path="admins" element={<V3Admins />} />
                <Route path="sso" element={<V3Sso />} />
                {/* project groups were retired (department → project → member) */}
                <Route path="project-groups" element={<Navigate to="/v3/projects" replace />} />
                <Route path="departments" element={<V3Departments />} />
                <Route path="workspaces" element={<V3Workspaces />} />
                <Route path="network" element={<V3NetworkProfiles />} />
                <Route path="storage" element={<V3Storage />} />
                <Route path="cost-tags" element={<V3CostTags />} />
                <Route path="projects" element={<V3Projects />} />
                <Route path="project-settings" element={<V3ProjectSettings />} />
                <Route path="models" element={<V3Models />} />
                <Route path="system-update" element={<V3SystemUpdate />} />
                <Route path="menu-settings" element={<V3MenuSettings />} />
                <Route path="identity" element={<V3Identity />} />
                <Route path="fleet" element={<V3Fleet />} />
                <Route path="announcements" element={<V3Announcements />} />
                <Route path="videos" element={<V3Videos />} />
                <Route path="video-management" element={<V3VideoManagement />} />
                <Route path="releases" element={<V3Releases />} />
                <Route path="issues" element={<V3Issues />} />
                <Route path="environments" element={<V3Environments />} />
                <Route path="observability" element={<V3Observability />} />
                <Route path="memory" element={<V3Memory />} />
                <Route path="governance" element={<V3Governance />} />
                <Route path="connections" element={<V3Connections />} />
                {/* the former Gateway tools page is the 工具目标 tab of Gateway governance */}
                <Route path="tools/gateway" element={<Navigate to="/v3/governance?tab=targets" replace />} />
                <Route path="costs" element={<V3Costs />} />
                <Route path="*" element={<Navigate to="/v3" replace />} />
              </Route>
              <Route
                path="v2"
                element={
                  <RouteChunk>
                    <V2Frame />
                  </RouteChunk>
                }
              >
                <Route index element={<V2Home />} />
                <Route path="agents" element={<V2Agents />} />
                <Route path="eval/data" element={<V2DataCenter />} />
                <Route path="eval/tasks" element={<V2Tasks />} />
                <Route path="eval/insights" element={<V2Insights />} />
                <Route path="eval/intents" element={<V2Intents />} />
                <Route path="eval/evaluators" element={<V2Evaluators />} />
                <Route path="eval/online" element={<V2OnlineRoute />} />
                <Route path="eval/experiments" element={<V2Experiments />} />
                <Route path="eval/standards" element={<V2Standards />} />
                <Route path="assistant" element={<V2Assistant />} />
                <Route path="registry" element={<V2Registry />} />
                <Route path="knowledge-bases" element={<V2KnowledgeBases />} />
                <Route path="context-layer" element={<V2ContextLayer />} />
                <Route path="skill-lab" element={<V2SkillLab />} />
                <Route path="coding" element={<V2Coding />} />
                <Route path="chat" element={<V2Chat />} />
                <Route path="observability" element={<V2Observability />} />
                <Route path="memory" element={<V2Memory />} />
                <Route path="governance" element={<V2Governance />} />
                <Route path="users" element={<V2Users />} />
                <Route path="costs" element={<V2Costs />} />
                <Route path="issues" element={<V2Issues />} />
                <Route path="fleet" element={<V2Fleet />} />
                <Route path="promotions" element={<V2Promotions />} />
                <Route path="environments" element={<V2Environments />} />
                <Route path="workspaces" element={<V2Workspaces />} />
                <Route path="connections" element={<V2Connections />} />
                <Route path="my-connections" element={<V2MyConnections />} />
                <Route path="announcements" element={<V2Announcements />} />
                <Route path="videos" element={<V2Videos />} />
                <Route path="video-management" element={<V2VideoManagement />} />
                <Route path="*" element={<V2NotFound />} />
              </Route>
              <Route element={<ConsoleShell />}>
              <Route index element={<IndexRoute />} />
              <Route path="agents" element={<AgentsRoute mode="list" />} />
              <Route path="agents/new" element={<AgentsNewRoute />} />
              <Route path="agents/import" element={<CreateAgent mode="import" />} />
              <Route path="agents/:agentId" element={<AgentsRoute mode="detail" />} />
              <Route path="agents/:agentId/edit" element={<AgentsEditRoute />} />
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
                <Route
                  path="users"
                  element={<V2OrClassic classic={<Users />} toV2={classicUsersToV2} />}
                />
                <Route
                  path="announcements"
                  element={<V2OrClassic classic={<Announcements />} toV2={classicAnnouncementsToV2} />}
                />
                <Route
                  path="videos"
                  element={<V2OrClassic classic={<Videos />} toV2={classicVideosToV2} />}
                />
                <Route
                  path="workspaces"
                  element={<V2OrClassic classic={<Workspaces />} toV2={classicWorkspacesToV2} />}
                />
                {/* Catch-all stays INSIDE the Shell group so an unknown URL keeps
                    the sidebar/topbar/footer instead of a bare background grid. */}
                <Route path="*" element={<NotFound />} />
              </Route>
            </Routes>
          </ProjectProvider>
        </WorkspaceProvider>
      </ToastProvider>
    </BrowserRouter>
  );
}
