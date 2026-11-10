// Deny one tool to members while administrators keep it (the Global
// `payout_admin_only` analogue; `principal in Role::"admin"` replaces the
// Cognito `cognito:groups` claim).
forbid(
  principal,
  action == Launchpad::Action::"call_tool",
  resource == Launchpad::Tool::"__TOOL_ID__"
) unless {
  principal in Launchpad::Role::"admin"
};
