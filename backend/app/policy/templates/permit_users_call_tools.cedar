// Baseline, Stage BT (the user behind the call): members, administrators,
// system principals (eval / canary tokens) and `/v1` API keys may call every
// tool — the API key stands where Global's M2M client does (covered by the
// baseline, narrowed by forbids such as `forbid(principal == Launchpad::ApiKey::"<id>", …)`).
// Narrow it with `forbid_tool_for_members` or a tool-specific permit instead of removing it.
permit(
  principal in Launchpad::Role::"member",
  action == Launchpad::Action::"call_tool",
  resource
);

permit(
  principal in Launchpad::Role::"admin",
  action == Launchpad::Action::"call_tool",
  resource
);

permit(
  principal is Launchpad::System,
  action == Launchpad::Action::"call_tool",
  resource
);

permit(
  principal is Launchpad::ApiKey,
  action == Launchpad::Action::"call_tool",
  resource
);
