"""Refresh the caller role map after deploying reference Agents; Cedar is unchanged."""

from lab import api, evidence, load, session

state = load()
policy = state["policies"]["itsm_agents_call_tools"]
result = api(
    session(),
    "POST",
    f"/api/policy-engines/{state['engine']['id']}/policies/{policy['id']}/publish",
    {
        "version": policy.get("active_version") or 1,
        "reason": "Workshop: refresh caller roles after Agent creation; unchanged Cedar.",
    },
)
evidence("caller-refresh.json", result)
print("Caller map refreshed; allow the Gateway configuration cache to refresh before testing.")
