from typing import Any
from strands import Agent, tool
import asyncio
import subprocess
import os
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

DEFAULT_SYSTEM_PROMPT = """你是一名面向中国大陆老人的简体中文陪伴助手。你的目标是耐心倾听、提供日常陪伴，并在安全边界内解释一般健康知识。

你不是医生，不得作出或排除个体医疗诊断，不得解读个人检查结果或制定治疗方案，不得建议开始、停止、更换、增减或加倍服用药物。不得编造医生、医院、指南、论文、研究、链接或其他健康依据。不得主动索取或声称长期保存病史、药物、地址、联系方式等敏感信息。

你不能联系家属、医护人员或急救机构，也不得声称已经报警、通知、发送消息或执行其他外部行动。遇到胸痛、严重呼吸困难、意识异常、自伤风险或其他可能紧急的情况，应把立即联系当地急救、家属、身边可信任的人或医护人员的建议放在回答前部，并明确不要继续依赖聊天等待。

面对含糊症状时，说明仅凭描述不能诊断，可提出少量必要的澄清问题。请求超出边界或依据不足时，透明说明限制并建议咨询合适的专业人员。

始终使用简短、清楚、温和、耐心、尊重的简体中文；先回应老人当前表达，不说教、不催促，不使用年龄刻板印象。"""


# Define a collection of tools used by the model
tools = []

_INLINE_FUNCTION_NAMES = set()

@tool
def shell(command: str, timeout: int = 300) -> dict:
    """Execute a bash command and return the results.

    Args:
        command: The bash command to execute
        timeout: Timeout in seconds (default: 300)

    Returns:
        Dict with stdout, stderr, and exit_code
    """
    result = subprocess.run(
        command, shell=True, capture_output=True, text=True, timeout=timeout
    )
    return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode}

tools.append(shell)
@tool
def file_operations(
    command: str,
    path: str,
    old_str: str = None,
    new_str: str = None,
    file_text: str = None,
    insert_line: int = None,
    view_range: list = None,
) -> str:
    """Text editor tool for viewing and modifying files.

    Args:
        command: The command to execute ("view", "str_replace", "create", "insert")
        path: Path to the file or directory
        old_str: Text to replace (for str_replace command)
        new_str: Replacement text (for str_replace and insert commands)
        file_text: Content for new file (for create command)
        insert_line: Line number to insert after (for insert command)
        view_range: [start_line, end_line] for viewing specific lines (for view command)

    Returns:
        Result of the operation
    """
    try:
        if command == "view":
            if not os.path.exists(path):
                return f"Error: Path '{path}' does not exist"
            if os.path.isdir(path):
                return "\n".join(os.listdir(path))
            with open(path) as f:
                lines = f.read().splitlines()
            if view_range:
                start, end = view_range
                start_idx = max(0, start - 1)
                end_idx = len(lines) if end == -1 else min(len(lines), end)
                lines = lines[start_idx:end_idx]
                start_num = start_idx + 1
            else:
                start_num = 1
            return "\n".join(f"{start_num + i}: {line}" for i, line in enumerate(lines))
        elif command == "str_replace":
            if old_str is None or new_str is None:
                return "Error: str_replace requires both old_str and new_str parameters"
            if not os.path.exists(path):
                return f"Error: File '{path}' does not exist"
            content = open(path).read()
            if old_str not in content:
                return "Error: Text not found in file"
            count = content.count(old_str)
            if count > 1:
                return f"Error: Text appears {count} times in file. Please be more specific."
            open(path, "w").write(content.replace(old_str, new_str, 1))
            return f"Successfully replaced text in '{path}'"
        elif command == "create":
            if file_text is None:
                return "Error: create requires file_text parameter"
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            open(path, "w").write(file_text)
            return f"Successfully created file '{path}'"
        elif command == "insert":
            if new_str is None or insert_line is None:
                return "Error: insert requires both new_str and insert_line parameters"
            if not os.path.exists(path):
                return f"Error: File '{path}' does not exist"
            lines = open(path).read().splitlines(True)
            if insert_line == 0:
                lines.insert(0, new_str + "\n")
            elif insert_line >= len(lines):
                lines.append(new_str + "\n")
            else:
                lines.insert(insert_line, new_str + "\n")
            open(path, "w").write("".join(lines))
            return f"Successfully inserted text in '{path}' at line {insert_line + 1}"
        else:
            return f"Error: Unknown command '{command}'"
    except Exception as e:
        return f"Error: {e}"

tools.append(file_operations)


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
                    max_iterations=1,

                    timeout_seconds=60,
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


    timeout_seconds = 60
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
