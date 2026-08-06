import os
import logging
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp.mcp_client import MCPClient

logger = logging.getLogger(__name__)

from bedrock_agentcore.identity import requires_access_token

@requires_access_token(
    provider_name="launchpad-gw-m2m",
    scopes=["launchpad-gw/invoke"],
    auth_flow="M2M",
)
def _get_bearer_token_launchpad_gw(*, access_token: str):
    """Obtain OAuth access token via AgentCore Identity for launchpad_gw."""
    return access_token

def get_launchpad_gw_mcp_client() -> MCPClient | None:
    """Returns an MCP Client connected to the launchpad_gw gateway."""
    url = os.environ.get("GATEWAY_GATEWAY_LAUNCHPAD_GW_EM0YUQMMDP_URL")
    if not url:
        logger.warning("GATEWAY_GATEWAY_LAUNCHPAD_GW_EM0YUQMMDP_URL not set — launchpad_gw gateway tools unavailable")
        return None
    token = _get_bearer_token_launchpad_gw()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return MCPClient(lambda: streamablehttp_client(url, headers=headers), prefix="launchpad_gw")

def get_all_gateway_mcp_clients() -> list[MCPClient]:
    """Returns MCP clients for all configured gateways."""
    clients = []
    client = get_launchpad_gw_mcp_client()
    if client:
        clients.append(client)
    return clients
