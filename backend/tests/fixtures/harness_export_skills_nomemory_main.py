# Reduced from a real @aws/agentcore 0.21.1 export (Skills on, Memory off).
# Prompt and S3 location are sanitized. Unused inline-tool helpers and the
# streaming timeout watchdog are omitted; factory and Skill loading are retained.
from strands import Agent, AgentSkills
from skills.fetcher import resolve_s3_skills
import asyncio
from strands.tools.executors import SequentialToolExecutor
from hooks.execution_limits import ExecutionLimitsHook
from strands.agent.conversation_manager.sliding_window_conversation_manager import SlidingWindowConversationManager
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from model.load import load_model

app = BedrockAgentCoreApp()
log = app.logger

# Define MCP clients for all configured MCP servers (gateways and/or remote MCP)
mcp_clients = []

DEFAULT_SYSTEM_PROMPT = """Use the product knowledge base and approved skills."""


# Define a collection of tools used by the model
tools = []

# Add MCP clients to tools
for mcp_client in mcp_clients:
    if mcp_client:
        tools.append(mcp_client)


def _make_conversation_manager():
    return SlidingWindowConversationManager(**{"window_size":150}, per_turn=True)

_agent = None

def get_or_create_agent(skill_plugins=None):
    global _agent
    if _agent is None:
        _agent = Agent(
            model=load_model(),
            system_prompt=DEFAULT_SYSTEM_PROMPT,
            tools=tools,
            conversation_manager=_make_conversation_manager(),
            plugins=skill_plugins or None,
            tool_executor=SequentialToolExecutor(),
            callback_handler=None,
            hooks=[
                ExecutionLimitsHook(
                    max_iterations=8,

                    timeout_seconds=180,
                ),
            ],
        )
    return _agent


def _extract_prompt(payload: dict):
    """Accept harness-style messages[], tool_results[], or plain prompt string payloads."""
    if "messages" in payload:
        return payload["messages"]
    if "tool_results" in payload:
        return [{"role": "user", "content": [{"toolResult": {
            "toolUseId": tr["toolUseId"],
            "status": tr.get("status", "success"),
            "content": tr.get("content", []),
        }} for tr in payload["tool_results"]]}]
    return payload.get("prompt", "")


@app.entrypoint
async def invoke(payload, context):
    log.info("Invoking Agent.....")

    skill_paths = []
    s3_skill_sources = ["s3://example-artifacts/skills/product-support/"]
    skill_paths.extend(await asyncio.to_thread(resolve_s3_skills, s3_skill_sources, None))
    _skill_plugins = [AgentSkills(skills=skill_paths)] if skill_paths else []

    agent = get_or_create_agent(_skill_plugins)

    prompt = _extract_prompt(payload)

    async for event in agent.stream_async(
        prompt,
    ):
        if not isinstance(event, dict) or "event" not in event:
            continue
        cbs = event["event"].get("contentBlockStart")
        if cbs is not None and not cbs.get("start"):
            continue
        yield event


if __name__ == "__main__":
    app.run()
