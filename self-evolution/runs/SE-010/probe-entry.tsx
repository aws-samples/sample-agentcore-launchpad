import { createRoot } from "react-dom/client";
import "/home/ubuntu/workspace/agentcore_launchpad-worktrees/evo-se-010/frontend/src/i18n";
import { VersionsPanel } from "/home/ubuntu/workspace/agentcore_launchpad-worktrees/evo-se-010/frontend/src/components/VersionsPanel";

createRoot(document.getElementById("root")!).render(<VersionsPanel agentId="a1" />);
