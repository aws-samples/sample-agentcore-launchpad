"""``recommendations.discover_agent_tools`` — toolName → description from an agent's own
spec (registry attachments, platform toolkits, ``@tool`` docstrings, promoted overrides;
gateway / MCP servers skipped). Ported from the retired experiment suite (N13-obs2): the
run-recommendation inputs are its only caller now."""

from app.evaluation import recommendations as recs


def test_discover_agent_tools_from_spec_and_code():
    spec = {
        "tools": [{"name": "search", "description": "Search the registry"}],
        "code": ('@tool\ndef calculator(expression: str) -> str:\n'
                 '    """Evaluate a basic arithmetic expression.\n\n'
                 '    Args:\n        expression: the math\n    """\n'
                 '    return ""\n'),
        "code_bundle": {
            "main.py": ('@tool\ndef shell(command: str, timeout: int = 300):\n'
                        '    """Execute a bash command and return the results.\n'
                        '    Args:\n        command: The bash command\n    """\n'),
            "notes.md": "no tools here",
        },
    }
    assert recs.discover_agent_tools(spec) == {
        "search": "Search the registry",
        "calculator": "Evaluate a basic arithmetic expression.",
        "shell": "Execute a bash command and return the results.",
    }
    assert recs.discover_agent_tools({}) == {}


def test_discover_agent_tools_promoted_overrides_win():
    assert recs.discover_agent_tools({
        "tools": [{"name": "search", "description": "old"}],
        "tool_description_overrides": {"search": "promoted", "shell": "new"},
    }) == {"search": "promoted", "shell": "new"}


def test_discover_agent_tools_reads_platform_toolkits():
    """A toolkit agent's spec.code is None by design, so the docstring regex can
    never see its tools — they come from the toolkit registry instead."""
    tools = recs.discover_agent_tools({"toolkits": ["hr_assistant"]})
    assert sorted(tools) == [
        "get_benefits_summary",
        "get_pay_stub",
        "get_pto_balance",
        "lookup_hr_policy",
        "submit_pto_request",
    ]
    assert tools["get_pto_balance"].startswith("Return the current PTO balance")
    # the template's always-emitted tools are NOT expected for a toolkit agent
    assert "calculator" not in tools and "current_utc_time" not in tools
    # promoted overrides still win over the toolkit default
    promoted = recs.discover_agent_tools({
        "toolkits": ["hr_assistant"],
        "tool_description_overrides": {"get_pay_stub": "promoted"},
    })
    assert promoted["get_pay_stub"] == "promoted"


def test_discover_agent_tools_skips_gateway_servers_but_keeps_builtins():
    spec = {
        "tools": [
            {"type": "gateway", "name": "hr-database"},
            {"type": "mcp", "name": "deepwiki"},
            {"type": "builtin", "name": "code-interpreter"},
            {"name": "legacy-attachment", "description": "no type field"},
        ],
        "toolkits": ["hr_assistant"],
    }
    tools = recs.discover_agent_tools(spec)
    # server names would never be observed -> would pin readiness at "sparse"
    assert "hr-database" not in tools and "deepwiki" not in tools
    # a builtin's name IS what the model calls
    assert "code-interpreter" in tools
    # an untyped entry keeps its old behaviour
    assert tools["legacy-attachment"] == "no type field"
    assert "get_pto_balance" in tools
