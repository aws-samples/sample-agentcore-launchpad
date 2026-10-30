// Baseline, Stage A (the agent): every agent of this workspace may call the
// Gateway tools it declares. The gateway interceptor decides each call; Cedar's
// default-deny means a workspace with the interceptor attached and no permit
// denies every call — publish this first, then `permit_users_call_tools`.
permit(
  principal in Launchpad::Workspace::"__WORKSPACE_ID__",
  action == Launchpad::Action::"call_tool",
  resource
);
