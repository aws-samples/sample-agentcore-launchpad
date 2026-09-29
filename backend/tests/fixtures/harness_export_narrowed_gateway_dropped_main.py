from typing import Any
from strands import Agent, tool
import asyncio
from strands.tools.executors import SequentialToolExecutor
from strands.types.exceptions import EventLoopException
from hooks.execution_limits import ExecutionLimitExceeded, ExecutionLimitsHook
from strands.agent.conversation_manager.sliding_window_conversation_manager import SlidingWindowConversationManager
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from model.load import load_model

app = BedrockAgentCoreApp()
log = app.logger

# Define MCP clients for all configured MCP servers (gateways and/or remote MCP)
mcp_clients = []

DEFAULT_SYSTEM_PROMPT = """You are the AnyCompany HR policy assistant."""


# Define a collection of tools used by the model
tools = []

_INLINE_FUNCTION_NAMES = set()



# Add MCP clients to tools
for mcp_client in mcp_clients:
    if mcp_client:
        tools.append(mcp_client)


def _make_conversation_manager():
    return SlidingWindowConversationManager(**{"window_size":150}, per_turn=True)

_agent = None

def get_or_create_agent():
    global _agent
    if _agent is None:
        _agent = Agent(
            model=load_model(),
            system_prompt=DEFAULT_SYSTEM_PROMPT,
            tools=tools,
            conversation_manager=_make_conversation_manager(),
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


def _has_inline_function_call(messages) -> bool:
    """Return True if messages contains an assistant toolUse for an inline function tool."""
    if not _INLINE_FUNCTION_NAMES or not isinstance(messages, list):
        return False
    for msg in messages:
        if msg.get("role") == "assistant":
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("toolUse", {}).get("name") in _INLINE_FUNCTION_NAMES:
                    return True
    return False


def _is_inline_function_call(event: dict) -> bool:
    """Check if a contentBlockStart event is for an inline function tool."""
    if not _INLINE_FUNCTION_NAMES:
        return False
    cbs = event.get("contentBlockStart", {})
    start = cbs.get("start", {})
    tool_use = start.get("toolUse") if isinstance(start, dict) else None
    return tool_use is not None and tool_use.get("name") in _INLINE_FUNCTION_NAMES



@app.entrypoint
async def invoke(payload, context):
    log.info("Invoking Agent.....")


    agent = get_or_create_agent()

    prompt = _extract_prompt(payload)


    timeout_seconds = 180
    timeout_fired = False
    watchdog_task = None
    if timeout_seconds is not None:
        async def _timeout_watchdog():
            nonlocal timeout_fired
            await asyncio.sleep(timeout_seconds)
            timeout_fired = True
            agent.cancel()
        watchdog_task = asyncio.create_task(_timeout_watchdog())

    try:
        async for event in agent.stream_async(
            prompt,
        ):
            if not isinstance(event, dict) or "event" not in event:
                continue
            cbs = event["event"].get("contentBlockStart")
            if cbs is not None and not cbs.get("start"):
                continue
            yield event

        if timeout_fired:
            yield {"event": {"messageStop": {"stopReason": "timeout_exceeded"}}}
    except EventLoopException as e:
        if isinstance(e.original_exception, ExecutionLimitExceeded):
            yield {"event": {"messageStop": {"stopReason": str(e.original_exception)}}}
            return
        raise
    finally:
        if watchdog_task is not None:
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    app.run()
