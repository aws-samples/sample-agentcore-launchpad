import { lazy } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";

import { ToastProvider } from "./components";
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
const CreateAgentStudio = lazy(() =>
  import("./pages/CreateAgentStudio").then((m) => ({ default: m.CreateAgentStudio })),
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
const Workspaces = lazy(() =>
  import("./pages/Workspaces").then((m) => ({ default: m.Workspaces })),
);

export default function App() {
  return (
    <BrowserRouter>
      <ToastProvider>
        <WorkspaceProvider>
          <Routes>
            <Route element={<Shell />}>
              <Route index element={<Overview />} />
              <Route path="create" element={<CreateAgent />} />
              <Route path="create/studio" element={<CreateAgentStudio />} />
              <Route path="registry" element={<Registry />} />
              <Route path="knowledge-bases" element={<KnowledgeBases />} />
              <Route path="memory" element={<Memory />} />
              <Route path="chat" element={<Chat />} />
              <Route path="observability" element={<Observability />} />
              <Route path="evaluation" element={<Evaluation />} />
              <Route path="skill-lab" element={<SkillLab />} />
              <Route path="governance" element={<Governance />} />
              <Route path="users" element={<Users />} />
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
