forbid (
  principal is AgentCore::User,
  action in [
    AgentCore::Action::"servicenow___search_tickets",
    AgentCore::Action::"servicenow___get_ticket",
    AgentCore::Action::"servicenow___create_incident",
    AgentCore::Action::"servicenow___add_comment",
    AgentCore::Action::"servicenow___escalate",
    AgentCore::Action::"servicenow___resolve_ticket"
  ],
  resource
) when {
  context.input.requester != principal.username
} unless {
  principal == AgentCore::User::"itdesk01"
};
