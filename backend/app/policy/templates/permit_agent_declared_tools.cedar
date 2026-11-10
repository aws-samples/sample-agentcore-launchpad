// Per agent: allow exactly the Gateway tools its spec declares. The editor
// prefill substitutes the agent id and one `like` clause per declared
// `<record>___<tool>` target; `*` keeps a whole record's target.
permit(
  principal == Launchpad::Agent::"__AGENT_ID__",
  action == Launchpad::Action::"call_tool",
  resource
) when {
  resource.name like "__TOOL_PATTERN__"
};
