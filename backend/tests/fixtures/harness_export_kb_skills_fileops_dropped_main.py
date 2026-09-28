from typing import Any
from strands import Agent, tool
from strands import AgentSkills
from skills.fetcher import resolve_s3_skills, resolve_git_skills
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

DEFAULT_SYSTEM_PROMPT = """你是面向 AnyCompany 员工的中文境内差旅报销助手。基于已挂载的制度知识库回答政策问题，并遵循 travel-expense-precheck Skill 生成可复制的预审摘要；完整预审时读取 references/output-template.md。

每个政策派生数字都必须由当前知识库证据支持，并注明制度编号和具体条款。制度冲突按 FIN-TR-001、FIN-TR-002、FIN-TR-003 的顺序处理。缺少必要信息时先追问；找不到有效条款、职级标准或无法消解冲突时，不猜测、不计算，建议联系财务 BP。名单外的境内城市按制度规定的三类城市处理，不得直接视为未知城市。

你只提供预审，不保存、提交或修改报销记录，不承诺审批结果。境外差旅和业务招待不在范围内，应说明边界并建议联系财务 BP。不要主动索取姓名、员工号、票据或银行卡等非必要信息。始终使用清晰、专业、简洁的中文。
## Knowledge bases
Retrieval tools are mounted for you. Prefer `agentic-anycompany-travel-assistant___AgenticRetrieveStream`
(multi-step retrieval across every mounted knowledge base, returns a cited
answer) for open questions; use a per-KB `…___Retrieve` tool for a targeted
single search. Mounted knowledge bases:
- anycompany-travel-policy (tool `anycompany-travel-policy-esmvfdcuxs___Retrieve`) — AnyCompany 境内差旅制度：职级分组、城市分级、交通/住宿/伙食补助/市内交通标准、报销时限、票据要求与审批路径（FIN-TR-001/002/003）。
Ground answers on retrieved content and cite sources when you use them."""


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

    skill_paths = []
    s3_skill_sources = ["s3://launchpad-artifacts-111122223333-us-east-1/assistant-skills/f1c5082723adcf0f/"]
    skill_paths.extend(await asyncio.to_thread(resolve_s3_skills, s3_skill_sources, None))
    _skill_plugins = [AgentSkills(skills=skill_paths)] if skill_paths else []

    agent = get_or_create_agent(_skill_plugins)

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
