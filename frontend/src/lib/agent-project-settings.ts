import type { AgentProjectSettings } from "./api";

type T = (key: string, opts?: Record<string, unknown>) => string;

/**
 * "Current value comes from: …" for the agent-project switch (console > env >
 * launchpad.yaml > default, `services/agent_project_settings`). Shared by the V2
 * workspaces card and the V3 projects panel.
 */
export function agentProjectSettingsSource(view: AgentProjectSettings, t: T): string {
  const base = "v3.create.project.settings";
  const source =
    view.source === "console"
      ? t(`${base}.sourceConsole`, { by: view.updated_by || "—" })
      : view.source === "env"
        ? t(`${base}.sourceEnv`, { env: view.env })
        : view.source === "config"
          ? t(`${base}.sourceConfig`)
          : t(`${base}.sourceDefault`);
  return t(`${base}.source`, { source });
}

/** Turning the switch ON widens where agents land: the UI confirms first. */
export function needsConfirm(current: boolean, next: boolean): boolean {
  return next && !current;
}
