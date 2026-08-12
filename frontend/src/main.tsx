import "@fontsource-variable/archivo/wdth.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "@fontsource/ibm-plex-mono/600.css";
import "./theme/tokens.css";
import "./theme/app.css";
import "./i18n";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { AuthGate } from "./auth/AuthGate";
import { installWorkspaceHeader } from "./lib/workspace-header";

// Before the first render, so no mount effect can reach the backend without
// naming the workspace it means.
installWorkspaceHeader();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthGate>
      <App />
    </AuthGate>
  </StrictMode>,
);
