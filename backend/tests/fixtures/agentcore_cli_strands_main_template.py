# Trimmed from @aws/agentcore 0.21.1 dist/assets/python/http/strands/base/main.py:
# only the regions gated on hasShell / hasFileOperations (imports + tool blocks).
from strands import Agent, tool
{{#if hasShell}}
import subprocess
{{/if}}
{{#if hasFileOperations}}
import os
{{/if}}
{{#if hasShell}}
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
{{/if}}
{{#if hasFileOperations}}
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
{{/if}}
