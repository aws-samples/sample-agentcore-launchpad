"""Harness → runtime graft library: graft anchors, env/requirements helpers,
bundle packaging, and the self-built Harness renderers (N7)."""

import ast
from pathlib import Path

import pytest

import app.services.harness_convert as hc
from app.deployer.zip_runtime import write_bundle_files
from app.schemas.agent import AgentSpec

FIXTURES = Path(__file__).parent / "fixtures"
MAIN_PY = (FIXTURES / "harness_export_main.py").read_text()
SKILLS_NOMEMORY_MAIN_PY = (FIXTURES / "harness_export_skills_nomemory_main.py").read_text()
PYPROJECT = (FIXTURES / "harness_export_pyproject.toml").read_text()
MCP_CLIENT_PY = (FIXTURES / "harness_export_mcp_client.py").read_text()


# ─── graft ───────────────────────────────────────────────────────────────────
def test_graft_inserts_bundle_contract_on_real_export():
    grafted = hc.graft_config_bundle(MAIN_PY)
    assert hc.GRAFT_START in grafted and hc.GRAFT_END in grafted
    assert "def resolve_system_prompt()" in grafted
    assert "def _launchpad_apply_tool_descriptions(agent)" in grafted
    assert "_launchpad_apply_tool_descriptions(agent)" in grafted
    assert "system_prompt=resolve_system_prompt()" in grafted
    assert "system_prompt=DEFAULT_SYSTEM_PROMPT" not in grafted
    # the baked default remains the fallback
    assert "DEFAULT_SYSTEM_PROMPT = " in grafted
    # helpers land AFTER the constant, BEFORE its first use
    assert grafted.index("def resolve_system_prompt") < grafted.index(
        "system_prompt=resolve_system_prompt()"
    )
    assert grafted.count("def resolve_system_prompt") == 1


@pytest.mark.parametrize(
    ("skill_export", "arguments"),
    [
        pytest.param(
            MAIN_PY.replace(
                "agent = get_or_create_agent(session_id, user_id)",
                "agent = get_or_create_agent(session_id, user_id, _skill_plugins)",
            ),
            "session_id, user_id, _skill_plugins",
            id="skills-with-memory",
        ),
        pytest.param(SKILLS_NOMEMORY_MAIN_PY, "_skill_plugins", id="skills-without-memory"),
    ],
)
def test_graft_supports_skill_plugin_agent_factory_call(skill_export, arguments):
    grafted = hc.graft_config_bundle(skill_export)

    compile(grafted, "main.py", "exec")
    assignment = f"agent = get_or_create_agent({arguments})"
    assert f"{assignment}\n    _launchpad_apply_tool_descriptions(agent)" in grafted
    assert "system_prompt=resolve_system_prompt()" in grafted
    assert "system_prompt=DEFAULT_SYSTEM_PROMPT" not in grafted
    assert hc.graft_config_bundle(grafted) == grafted


def test_graft_supports_real_export_without_memory_or_skills():
    source = (FIXTURES / "harness_export_nomemory_main.py").read_text()

    grafted = hc.graft_config_bundle(source)

    compile(grafted, "main.py", "exec")
    assert "agent = get_or_create_agent()\n    _launchpad_apply_tool_descriptions(agent)" in grafted
    assert "system_prompt=resolve_system_prompt()" in grafted
    assert "system_prompt=DEFAULT_SYSTEM_PROMPT" not in grafted
    assert hc.graft_config_bundle(grafted) == grafted


def test_graft_is_idempotent_and_upgrades_promoted_defaults():
    first = hc.graft_config_bundle(MAIN_PY)
    upgraded = hc.graft_config_bundle(
        first,
        default_system_prompt="promoted prompt",
        tool_description_overrides={"shell": "promoted shell description"},
    )
    assert upgraded.count(hc.GRAFT_START) == 1
    assert upgraded.count("def resolve_system_prompt") == 1
    assert upgraded.count("_launchpad_apply_tool_descriptions(agent)") == 2
    assert "_LAUNCHPAD_DEFAULT_SYSTEM_PROMPT = 'promoted prompt'" in upgraded
    assert "'shell': 'promoted shell description'" in upgraded


def test_graft_upgrades_legacy_prompt_only_block():
    legacy = MAIN_PY.replace(
        'system_prompt=DEFAULT_SYSTEM_PROMPT',
        'system_prompt=resolve_system_prompt()',
    )
    match = hc._PROMPT_CONST_RE.search(legacy)
    quote = legacy[match.end() - 3:match.end()]
    const_end = legacy.index(quote, match.end()) + 3
    legacy = legacy[:const_end] + """

# ─── Launchpad platform contract: config bundles (A/B experiments) ───────────
def resolve_system_prompt():
    return DEFAULT_SYSTEM_PROMPT
# ──────────────────────────────────────────────────────────────────────────────
""" + legacy[const_end:]
    upgraded = hc.graft_config_bundle(legacy)
    assert hc.GRAFT_START in upgraded
    assert "Launchpad platform contract: config bundles" not in upgraded
    assert "_launchpad_apply_tool_descriptions(agent)" in upgraded


def test_graft_fails_without_anchors():
    with pytest.raises(hc.ConversionError, match="DEFAULT_SYSTEM_PROMPT constant"):
        hc.graft_config_bundle("print('no anchors here')")
    # constant present but construction site missing
    partial = 'DEFAULT_SYSTEM_PROMPT = """x"""\nagent = Agent()'
    with pytest.raises(hc.ConversionError, match="construction site"):
        hc.graft_config_bundle(partial)


@pytest.mark.parametrize(
    "replacement",
    [
        "other = get_or_create_agent(session_id, user_id)",
        "agent = get_or_create_agent(other_session, user_id)",
        "agent = make_agent(session_id, user_id)",
        "agent = get_or_create_agent(session_id)",
        "agent = get_or_create_agent(user_id)",
        "agent = get_or_create_agent(skill_plugins)",
        "agent = get_or_create_agent(other_plugins)",
        "agent = get_or_create_agent(skill_plugins=_skill_plugins)",
        "agent = get_or_create_agent(_skill_plugins=_skill_plugins)",
        "agent = get_or_create_agent(_skill_plugins, user_id=user_id)",
        "agent = get_or_create_agent(*_skill_plugins)",
        "agent = get_or_create_agent(session_id, user_id, other_plugins)",
        "agent = get_or_create_agent(session_id, user_id, _skill_plugins, extra)",
        "agent = get_or_create_agent(session_id=session_id, user_id=user_id)",
    ],
)
def test_graft_rejects_unrelated_agent_assignments(replacement):
    source = MAIN_PY.replace(
        "agent = get_or_create_agent(session_id, user_id)", replacement
    )

    with pytest.raises(hc.ConversionError, match="expected exactly one"):
        hc.graft_config_bundle(source)


def test_graft_rejects_malformed_generated_python():
    with pytest.raises(hc.ConversionError, match="invalid Python"):
        hc.graft_config_bundle(MAIN_PY + "\ndef broken(:\n")


def test_graft_rejects_ambiguous_agent_assignments():
    source = MAIN_PY + "\nagent = get_or_create_agent(session_id, user_id)\n"

    with pytest.raises(hc.ConversionError, match="expected exactly one"):
        hc.graft_config_bundle(source)


def test_graft_rejects_misplaced_or_duplicate_apply_calls():
    misplaced = MAIN_PY.replace(
        "    prompt = _extract_prompt(payload)",
        "    prompt = _extract_prompt(payload)\n"
        "    _launchpad_apply_tool_descriptions(agent)",
    )
    with pytest.raises(hc.ConversionError, match="immediately after"):
        hc.graft_config_bundle(misplaced)

    grafted = hc.graft_config_bundle(MAIN_PY)
    duplicate = grafted.replace(
        "    prompt = _extract_prompt(payload)",
        "    prompt = _extract_prompt(payload)\n"
        "    _launchpad_apply_tool_descriptions(agent)",
    )
    with pytest.raises(hc.ConversionError, match="exactly once"):
        hc.graft_config_bundle(duplicate)


@pytest.mark.parametrize(
    "replacement",
    [
        "if True: agent = get_or_create_agent(session_id, user_id)",
        "agent = get_or_create_agent(session_id, user_id); agent.cancel()",
    ],
)
def test_graft_rejects_agent_assignment_with_other_statements_on_line(replacement):
    source = MAIN_PY.replace(
        "agent = get_or_create_agent(session_id, user_id)", replacement
    )

    with pytest.raises(hc.ConversionError, match="own statement line"):
        hc.graft_config_bundle(source)


def test_direct_kb_graft_registers_both_tools_idempotently():
    grafted = hc.graft_direct_kb_tools(MAIN_PY)
    assert "from launchpad_kb_tools import kb_deep_search, kb_search" in grafted
    assert "tools.extend([kb_search, kb_deep_search])" in grafted
    assert grafted.index("tools = []") < grafted.index(
        "tools.extend([kb_search, kb_deep_search])"
    )
    assert hc.graft_direct_kb_tools(grafted) == grafted


def test_direct_kb_graft_fails_without_tools_collection_anchor():
    with pytest.raises(hc.ConversionError, match=r"tools = \[\] collection"):
        hc.graft_direct_kb_tools(MAIN_PY.replace("tools = []", "tools = [shell]"))


# ─── gateway soft-fail graft ─────────────────────────────────────────────────
def test_gateway_softfail_graft_wraps_the_module_scope_call():
    """The documented v1 blocker: the export builds its gateway clients at import,
    and requires_access_token raises in-container when no workload token exists. A
    crash there is worse than missing tools — the runtime never starts while the
    deploy pipeline still reports the agent active."""
    grafted = hc.graft_gateway_softfail(MAIN_PY)
    compile(grafted, "main.py", "exec")
    assert hc.GW_SOFTFAIL_START in grafted and hc.GW_SOFTFAIL_END in grafted
    assert "except Exception as _launchpad_gw_exc:" in grafted
    # the call itself survives, indented into the try
    assert "    mcp_clients += get_all_gateway_mcp_clients()" in grafted
    # and no bare module-scope call remains
    assert "\nmcp_clients += get_all_gateway_mcp_clients()" not in grafted


def test_gateway_softfail_graft_is_idempotent_and_preserves_layout():
    once = hc.graft_gateway_softfail(MAIN_PY)
    assert hc.graft_gateway_softfail(once) == once
    # the blank line after the call must survive (a greedy \s* tail would eat it)
    end = once.splitlines().index(hc.GW_SOFTFAIL_END)
    assert once.splitlines()[end + 1] == ""


def test_gateway_softfail_graft_fails_on_a_missing_anchor():
    """Same posture as the config-bundle graft: codegen drift FAILS the conversion
    rather than shipping a runtime that can crash at import."""
    with pytest.raises(hc.ConversionError, match="get_all_gateway_mcp_clients"):
        hc.graft_gateway_softfail(
            MAIN_PY.replace("mcp_clients += get_all_gateway_mcp_clients()", "pass")
        )


# ─── requirements flattening ────────────────────────────────────────────────
def test_flatten_requirements_dedupes_against_base_pins():
    files = {"pyproject.toml": PYPROJECT}
    base = ["bedrock-agentcore==1.17.*", "strands-agents==1.15.*", "boto3"]
    extras = hc.flatten_requirements(files, base)
    names = {e.split(">=")[0].split("[")[0].strip().lower() for e in extras}
    # base-pinned packages are excluded; export-only ones remain
    assert "bedrock-agentcore" not in names
    assert "strands-agents" not in names
    assert "mcp" in names
    assert "aws-opentelemetry-distro" in names


# ─── spec + packaging ────────────────────────────────────────────────────────
SKILL_URI = "s3://launchpad-artifacts-1-us-west-2/skills/lab-quota-answering/"


def test_discover_skills_reads_the_exported_source_lists():
    files = {
        "main.py": (
            "skill_paths = []\n"
            f's3_skill_sources = ["{SKILL_URI}"]\n'
            'git_skill_sources = ["https://example.invalid/repo.git"]\n'
        ),
        "notes.md": f's3_skill_sources = ["{SKILL_URI}"]',  # not python — ignored
    }
    s3_uris, other = hc.discover_skills(files)
    assert s3_uris == [SKILL_URI]
    assert other == ["https://example.invalid/repo.git"]


def test_code_bundle_validation():
    base = {"name": "x-agent", "method": "zip_runtime", "system_prompt": "p"}
    with pytest.raises(ValueError, match="main.py"):
        AgentSpec(**base, code_bundle={"other.py": "x"})
    with pytest.raises(ValueError, match="safe relative"):
        AgentSpec(**base, code_bundle={"main.py": "x", "../evil.py": "x"})
    with pytest.raises(ValueError, match="mutually exclusive"):
        AgentSpec(**base, code="single", code_bundle={"main.py": "x"})
    ok = AgentSpec(**base, code_bundle={"main.py": "x", "pkg/mod.py": "y"})
    assert ok.code_bundle["pkg/mod.py"] == "y"


def test_write_bundle_files_stages_subpackages(tmp_path):
    spec = AgentSpec(
        name="x-agent", method="zip_runtime", system_prompt="p",
        code_bundle={"main.py": "entry", "mcp_client/client.py": "mcp",
                     "memory/session.py": "mem"},
    )
    count = write_bundle_files(spec, tmp_path)
    assert count == 2  # main.py is the deployer's job
    assert (tmp_path / "mcp_client" / "client.py").read_text() == "mcp"
    assert (tmp_path / "memory" / "session.py").read_text() == "mem"
    assert not (tmp_path / "main.py").exists()


def test_flatten_sse_text_joins_deltas_and_raises_on_error():
    from app.services.agentcore.runtime import flatten_sse_text

    sse = (
        'data: {"event": {"messageStart": {"role": "assistant"}}}\n\n'
        'data: {"event": {"contentBlockDelta": {"delta": {"text": "Aurora "}}}}\n\n'
        'data: {"event": {"contentBlockDelta": {"delta": {"text": "Deck"}}}}\n\n'
    )
    assert flatten_sse_text(sse) == "Aurora Deck"
    assert flatten_sse_text('{"result": "plain json"}') is None
    assert flatten_sse_text("") is None
    with pytest.raises(RuntimeError, match="boom"):
        flatten_sse_text('data: {"event": {"runtimeClientError": "boom"}}\n')


# ─── lazy gateway token graft ────────────────────────────────────────────────
def test_lazy_gateway_token_graft_moves_the_fetch_into_the_transport():
    """The measured root cause of a tool-less converted runtime: the export bakes
    the Authorization header at MODULE scope, but the workload access token the M2M
    exchange needs is a per-request context value. MCPClient's transport callable is
    lazy (ToolProvider.load_tools), so moving just the fetch there is enough."""
    grafted = hc.graft_lazy_gateway_token(MCP_CLIENT_PY)
    compile(grafted, "client.py", "exec")
    assert hc.GW_LAZY_TOKEN_MARK in grafted
    # the fetch now lives inside the callable, not at return-statement level
    fetch_at = grafted.index("_token = _get_bearer_token_launchpad_gw()")
    def_at = grafted.index(f"def {hc.GW_LAZY_TOKEN_MARK}()")
    return_at = grafted.index(f"return {hc.GW_NO_BUNDLE_CLIENT}({hc.GW_LAZY_TOKEN_MARK}")
    assert def_at < fetch_at < return_at
    # no eager fetch survives
    assert "\n    token = _get_bearer_token_launchpad_gw()" not in grafted
    assert "lambda: streamablehttp_client(url, headers=headers)" not in grafted
    # the constructor keyword the CLI emitted is preserved
    assert 'prefix="launchpad_gw"' in grafted


def test_lazy_gateway_token_graft_is_idempotent():
    once = hc.graft_lazy_gateway_token(MCP_CLIENT_PY)
    assert hc.graft_lazy_gateway_token(once) == once


def test_lazy_gateway_token_graft_fails_on_a_missing_anchor():
    with pytest.raises(hc.ConversionError, match="_get_bearer_token"):
        hc.graft_lazy_gateway_token(
            MCP_CLIENT_PY.replace("token = _get_bearer_token_launchpad_gw()", "token = None")
        )


# ─── native Harness tools the export drops ──────────────────────────────────
# Live 2026-09-27: a Harness with allowedTools ['@launchpad_kb_gw', 'skills',
# 'file_operations'] exported without file_operations — the CLI only enables a
# builtin whose allowedTools entry matches `builtin/<name>` — so the Runtime twin
# could not read its Skill's references/output-template.md.
CLI_TEMPLATE = (FIXTURES / "agentcore_cli_strands_main_template.py").read_text()
# the real export of that Harness (account id redacted): no shell, no file_operations
DROPPED_MAIN_PY = (FIXTURES / "harness_export_kb_skills_fileops_dropped_main.py").read_text()


def test_native_graft_adds_the_cli_file_operations_tool_idempotently():
    assert "def file_operations(" not in DROPPED_MAIN_PY
    grafted = hc.graft_native_tools(DROPPED_MAIN_PY, ["file_operations"], CLI_TEMPLATE)

    compile(grafted, "main.py", "exec")
    assert grafted.count("def file_operations(") == 1
    assert grafted.count("tools.append(file_operations)") == 1
    assert "\nimport os\n" in grafted and "def shell(" not in grafted
    assert grafted.index("tools = []") < grafted.index(hc.NATIVE_GRAFT_START)
    assert grafted.index(hc.NATIVE_GRAFT_END) < grafted.index("Agent(")
    assert hc.graft_native_tools(grafted, ["file_operations"], CLI_TEMPLATE) == grafted


def test_native_graft_is_a_noop_when_the_export_kept_the_tool():
    exported = hc.graft_native_tools(DROPPED_MAIN_PY, ["shell"], CLI_TEMPLATE)
    marked = exported.replace(hc.NATIVE_GRAFT_START, "").replace(hc.NATIVE_GRAFT_END, "")
    assert hc.graft_native_tools(marked, ["shell"], CLI_TEMPLATE) == marked


def test_native_graft_fails_loudly_on_codegen_drift():
    with pytest.raises(hc.ConversionError, match="tools = \\[\\] collection"):
        drifted = DROPPED_MAIN_PY.replace("tools = []", "tools = [x]")
        hc.graft_native_tools(drifted, ["shell"], CLI_TEMPLATE)
    with pytest.raises(hc.ConversionError, match="no self-contained file_operations"):
        hc.graft_native_tools(DROPPED_MAIN_PY, ["file_operations"], "{{#if hasShell}}\n{{/if}}")


def test_grafted_file_operations_reads_a_skill_reference(tmp_path):
    block = hc.native_tool_block(CLI_TEMPLATE, "file_operations")
    scope: dict = {"tool": lambda fn: fn, "tools": []}
    exec("import os\n" + block, scope)  # noqa: S102 — the CLI's own template code
    ref = tmp_path / "references" / "output-template.md"
    ref.parent.mkdir()
    ref.write_text("| 项目 | 上限 |\n")

    assert "项目" in scope["file_operations"]("view", str(ref))
    assert scope["tools"] == [scope["file_operations"]]


# ─── Gateway clients the CLI export drops for a narrowed Harness ─────────────
NARROWED_MAIN = (FIXTURES / "harness_export_narrowed_gateway_dropped_main.py").read_text()
NARROWED_CLIENT = (FIXTURES / "harness_export_narrowed_gateway_dropped_client.py").read_text()
GW_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/launchpad-gw-a1b2c3d4e5"
KB_GW_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/launchpad-kb-gw-f6g7h8i9j0"


def _gw_tool(name, arn):
    return {"type": "agentcore_gateway", "name": name, "config": {"agentCoreGateway": {
        "gatewayArn": arn,
        "outboundAuth": {"oauth": {
            "providerArn": "arn:aws:bedrock-agentcore:us-east-1:1:token-vault/default/"
                           "oauth2credentialprovider/launchpad-gw-m2m",
            "scopes": ["launchpad-gw/invoke"], "grantType": "CLIENT_CREDENTIALS"}},
    }}}


NARROWED_HARNESS = {
    "tools": [_gw_tool("launchpad_gw", GW_ARN), _gw_tool("launchpad_kb_gw", KB_GW_ARN)],
    "allowedTools": ["@launchpad_gw/anyhr-leave___get_leave_balance",
                     "@launchpad_gw/anyhr-leave___list_leave_requests", "@launchpad_kb_gw"],
}


def _narrowed_files():
    return {"main.py": NARROWED_MAIN, "mcp_client/client.py": NARROWED_CLIENT,
            "pyproject.toml": PYPROJECT}


def test_real_narrowed_export_carries_no_gateway_client():
    """The recorded CLI 0.21.1 export of a Harness whose allowedTools are target-scoped
    selectors: the client module is an empty scaffold and main never calls it."""
    assert "MCPClient(" not in NARROWED_CLIENT
    assert "get_all_gateway_mcp_clients" not in NARROWED_MAIN
    assert "mcp_clients = []" in NARROWED_MAIN


def test_missing_gateway_clients_are_rebuilt_with_the_harness_narrowing():
    grafted = hc.graft_missing_gateway_clients(
        _narrowed_files(), NARROWED_HARNESS["tools"], NARROWED_HARNESS["allowedTools"],
        skip_gateway_arns=frozenset([KB_GW_ARN]),
    )
    client = grafted["mcp_client/client.py"]
    compile(client, "client.py", "exec")
    compile(grafted["main.py"], "main.py", "exec")
    assert hc.GW_CLIENTS_MARK in client
    assert 'os.environ.get("GATEWAY_GATEWAY_LAUNCHPAD_GW_A1B2C3D4E5_URL")' in client
    assert "provider_name='launchpad-gw-m2m'" in client
    assert ("tool_filters={'allowed': ['anyhr-leave___get_leave_balance', "
            "'anyhr-leave___list_leave_requests']}") in client
    assert "launchpad_kb_gw" not in client  # replaced by direct KB retrieval
    assert "from bedrock_agentcore.identity import requires_access_token" in client
    assert client.count("from strands.tools.mcp.mcp_client import MCPClient") == 1
    assert grafted["main.py"].count("mcp_clients += get_all_gateway_mcp_clients()") == 1
    # idempotent
    assert hc.graft_missing_gateway_clients(
        grafted, NARROWED_HARNESS["tools"], NARROWED_HARNESS["allowedTools"],
    ) == grafted


@pytest.mark.parametrize("allowed, expect_filter", [
    (["*"], False), (["@launchpad_gw"], False), (None, False),
    (["@launchpad_gw/anyhr-leave___*"], True),
])
def test_rebuilt_gateway_client_filter_follows_allowed_tools(allowed, expect_filter):
    grafted = hc.graft_missing_gateway_clients(
        _narrowed_files(), [_gw_tool("launchpad_gw", GW_ARN)], allowed,
    )
    client = grafted["mcp_client/client.py"]
    compile(client, "client.py", "exec")
    assert ("tool_filters" in client) is expect_filter
    if allowed == ["@launchpad_gw/anyhr-leave___*"]:
        assert "import re" in client and "re.compile(" in client


def test_unselected_or_already_exported_gateways_are_left_alone():
    files = _narrowed_files()
    assert hc.graft_missing_gateway_clients(
        files, [_gw_tool("launchpad_gw", GW_ARN)], ["@other_gw"],
    ) == files
    exported = {**files, "mcp_client/client.py": (FIXTURES / "harness_export_mcp_client.py")
                .read_text()}
    assert hc.graft_missing_gateway_clients(
        exported, [_gw_tool("launchpad_gw", GW_ARN)], None,
    ) == exported


def test_twin_gateway_client_drops_config_bundle_baggage():
    """Behind the experiment Gateway the twin receives the routed bundle as OTel
    baggage; launchpad-gw 400s tools/list when that baggage reaches it (live,
    2026-09-30). Every session start and background call must run without it."""
    from opentelemetry import baggage, context

    grafted = hc.graft_lazy_gateway_token(MCP_CLIENT_PY)
    start = grafted.index("_LAUNCHPAD_BUNDLE_BAGGAGE = ")
    end = grafted.rindex("context.detach(token)\n") + len("context.detach(token)\n")
    seen: list[dict] = []

    class FakeMCPClient:
        def start(self):
            seen.append(dict(baggage.get_all()))

        def _invoke_on_background_thread(self, coro):
            seen.append(dict(baggage.get_all()))
            return coro

    namespace = {"MCPClient": FakeMCPClient}
    exec(compile(grafted[start:end], "client.py", "exec"), namespace)  # noqa: S102
    client = namespace[hc.GW_NO_BUNDLE_CLIENT]()
    ctx = context.get_current()
    for key, value in (("aws.agentcore.configbundle_arn", "arn:bundle"),
                       ("aws.agentcore.configbundle_version", "v1"), ("keep", "me")):
        ctx = baggage.set_baggage(key, value, context=ctx)
    token = context.attach(ctx)
    try:
        client.start()
        assert client._invoke_on_background_thread("coro") == "coro"
        after = dict(baggage.get_all())
    finally:
        context.detach(token)
    assert seen == [{"keep": "me"}, {"keep": "me"}]
    assert after["aws.agentcore.configbundle_arn"] == "arn:bundle"  # caller untouched


# ─── AWS_IAM Gateways (e.g. a Registry-attached remote MCP behind its own Gateway) ─
IAM_GW_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:gateway/web-search-gw-k1l2m3n4o5"


def _iam_gw_tool(name, arn=IAM_GW_ARN):
    return {"type": "agentcore_gateway", "name": name, "config": {"agentCoreGateway": {
        "gatewayArn": arn, "outboundAuth": {"awsIam": {}}}}}


IAM_ALLOWED = ["@web_search/web-search-tool___WebSearch"]


def test_iam_gateway_client_is_rebuilt_with_a_sigv4_transport():
    """Live 2026-09-30: converting a Harness whose only Gateway is AWS_IAM failed with
    'has no OAuth outbound auth'. The rebuilt client signs instead of minting a token."""
    grafted = hc.graft_missing_gateway_clients(
        _narrowed_files(), [_iam_gw_tool("web_search")], IAM_ALLOWED,
    )
    client = grafted["mcp_client/client.py"]
    compile(client, "client.py", "exec")
    assert ("url = 'https://web-search-gw-k1l2m3n4o5.gateway.bedrock-agentcore."
            "us-east-1.amazonaws.com/mcp'") in client
    assert f"{hc.GW_SIGV4_TRANSPORT}(url, 'us-east-1'), prefix='web_search'" in client
    assert "tool_filters={'allowed': ['web-search-tool___WebSearch']}" in client
    assert "requires_access_token" not in client
    assert "GATEWAY_GATEWAY_" not in client  # no env contract for a non-shared Gateway
    assert grafted["main.py"].count("mcp_clients += get_all_gateway_mcp_clients()") == 1

    lazy = hc.graft_lazy_gateway_token(client)
    compile(lazy, "client.py", "exec")
    assert f"return {hc.GW_NO_BUNDLE_CLIENT}(lambda: {hc.GW_SIGV4_TRANSPORT}(" in lazy
    assert f"class {hc.GW_NO_BUNDLE_CLIENT}(MCPClient)" in lazy
    assert hc.graft_lazy_gateway_token(lazy) == lazy  # idempotent


def test_sigv4_transport_signs_each_request_for_the_gateway_region():
    import httpx

    grafted = hc.graft_missing_gateway_clients(
        _narrowed_files(), [_iam_gw_tool("web_search")], IAM_ALLOWED,
    )
    source = hc._SIGV4_TRANSPORT_SOURCE
    assert source in grafted["mcp_client/client.py"]

    class Frozen:
        access_key, secret_key, token = "AKIDEXAMPLE", "secret", "session-token"

    class Session:
        def get_credentials(self):
            return type("C", (), {"get_frozen_credentials": lambda self: Frozen()})()

    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    seen = {}
    scope = {"httpx": httpx, "boto3": type("B", (), {"Session": Session}),
             "SigV4Auth": SigV4Auth, "AWSRequest": AWSRequest,
             "streamablehttp_client": lambda url, auth: seen.update(url=url, auth=auth)}
    exec(source, scope)
    scope[hc.GW_SIGV4_TRANSPORT]("https://gw.example/mcp", "us-east-1")
    request = httpx.Request("POST", "https://gw.example/mcp",
                            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                            headers={"Connection": "keep-alive"})
    flow = seen["auth"].auth_flow(request)
    signed = next(flow)
    auth = signed.headers["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-east-1/bedrock-agentcore/aws4_request" in auth
    assert "connection" not in auth.split("SignedHeaders=")[1].split(",")[0]
    assert signed.headers["x-amz-security-token"] == "session-token"
    assert "x-amz-date" in signed.headers


def test_mixed_oauth_and_iam_gateways_are_both_rebuilt():
    grafted = hc.graft_missing_gateway_clients(
        _narrowed_files(), [_gw_tool("launchpad_gw", GW_ARN), _iam_gw_tool("web_search")],
        ["@launchpad_gw/anyhr-leave___get_leave_balance", *IAM_ALLOWED],
    )
    client = grafted["mcp_client/client.py"]
    assert "from bedrock_agentcore.identity import requires_access_token" in client
    assert hc.GW_SIGV4_TRANSPORT + "(url, 'us-east-1')" in client
    lazy = hc.graft_lazy_gateway_token(client)
    compile(lazy, "client.py", "exec")
    assert hc.GW_LAZY_TOKEN_MARK in lazy
    assert lazy.count(f"return {hc.GW_NO_BUNDLE_CLIENT}(") == 2


def test_gateway_without_any_outbound_auth_still_fails_the_conversion():
    tool = {"type": "agentcore_gateway", "name": "bare", "config": {"agentCoreGateway": {
        "gatewayArn": IAM_GW_ARN, "outboundAuth": {}}}}
    with pytest.raises(hc.ConversionError, match="neither OAuth nor AWS_IAM"):
        hc.graft_missing_gateway_clients(_narrowed_files(), [tool], None)


# ─── self-built Harness renderers (N7) ───────────────────────────────────────
HARNESS_TEMPLATE_DIR = Path(__file__).parent.parent / "app" / "templates" / "harness_runtime"
MIN_TEMPLATE = (FIXTURES / "harness_runtime_min" / "main.py.tmpl").read_text()
REAL_MAIN_PY = (HARNESS_TEMPLATE_DIR / "main.py.tmpl").read_text()
REAL_CLIENT_PY = (HARNESS_TEMPLATE_DIR / "mcp_client" / "client.py.tmpl").read_text()


@pytest.mark.parametrize("template", [MIN_TEMPLATE, REAL_MAIN_PY], ids=["fixture", "real"])
def test_actor_graft_reads_the_platform_actor_from_the_payload(template):
    out = hc.graft_actor_from_payload(template)
    assert (
        'user_id = str(payload.get("actor_id") or '
        'getattr(context, "user_id", "default-user"))'
    ) in out
    assert "getattr(context, 'user_id', 'default-user')" not in out
    assert hc.graft_actor_from_payload(out) == out  # idempotent
    with pytest.raises(hc.ConversionError, match="A7"):
        hc.graft_actor_from_payload(template.replace("user_id = getattr", "uid = getattr"))


@pytest.mark.parametrize("template", [MIN_TEMPLATE, REAL_MAIN_PY], ids=["fixture", "real"])
def test_loop_bounds_are_rewritten_everywhere_the_export_bakes_them(template):
    out = hc.render_loop_bounds(template, 12, 240)
    assert "max_iterations=12," in out and "max_iterations=10," not in out
    assert out.count("timeout_seconds=240,") == 1 and "timeout_seconds = 240" in out
    assert "timeout_seconds = 300" not in out
    with pytest.raises(hc.ConversionError, match="A8"):
        hc.render_loop_bounds(template.replace("ExecutionLimitsHook(", "LimitsHook("), 1, 10)


@pytest.mark.parametrize("template", [MIN_TEMPLATE, REAL_MAIN_PY], ids=["fixture", "real"])
def test_skill_sources_are_rendered_from_the_spec_and_readable_back(template):
    out = hc.render_skill_sources(template, ["s3://b/skills/x/", "s3://b/skills/y/"])
    assert "s3_skill_sources = ['s3://b/skills/x/', 's3://b/skills/y/']" in out
    assert hc.discover_skills({"main.py": out}) == (["s3://b/skills/x/", "s3://b/skills/y/"], [])
    assert "s3_skill_sources = []" in hc.render_skill_sources(template, [])
    with pytest.raises(hc.ConversionError, match="A6"):
        hc.render_skill_sources(template.replace("s3_skill_sources", "sources"), [])


@pytest.mark.parametrize(
    ("main_template", "client_template"),
    [(MIN_TEMPLATE, NARROWED_CLIENT), (REAL_MAIN_PY, REAL_CLIENT_PY)],
    ids=["fixture", "real"],
)
def test_remote_mcp_tools_are_rebuilt_as_soft_clients_next_to_the_sigv4_gateway(
    main_template, client_template
):
    files = {"main.py": main_template, "mcp_client/client.py": client_template}
    tools = [
        _iam_gw_tool("launchpad_gw",
                     "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:gateway/launchpad-gw-a1b2c3d4e5"),
        {"type": "remote_mcp", "name": "aws_knowledge",
         "config": {"remoteMcp": {"url": "https://knowledge-mcp.global.api.aws"}}},
    ]
    out = hc.graft_missing_gateway_clients(files, tools, None)
    client = out["mcp_client/client.py"]
    assert "def get_launchpad_gw_mcp_client" in client and hc.GW_SIGV4_TRANSPORT in client
    assert "def get_aws_knowledge_mcp_client" in client
    assert "streamablehttp_client('https://knowledge-mcp.global.api.aws')" in client
    assert "amazonaws.com.cn/mcp" in client  # partition-aware URL from the aws-cn ARN
    lazy = hc.graft_lazy_gateway_token(client)
    assert f"return {hc.GW_SOFT_CLIENT}(lambda: streamablehttp_client(" in lazy
    assert f"class {hc.GW_SOFT_CLIENT}({hc.GW_NO_BUNDLE_CLIENT})" in lazy
    assert hc.GW_SOFTFAIL_START in hc.graft_gateway_softfail(out["main.py"])


@pytest.mark.parametrize("template", [MIN_TEMPLATE, REAL_MAIN_PY], ids=["fixture", "real"])
def test_tool_module_graft_is_generic_and_idempotent(template):
    out = hc.graft_tool_module(
        template, start=hc.BUILTIN_GRAFT_START, end=hc.BUILTIN_GRAFT_END,
        import_line="from launchpad_builtin_tools import code_interpreter",
        tool_names=["code_interpreter"],
    )
    assert "tools.extend([code_interpreter])" in out
    assert hc.graft_tool_module(out, start=hc.BUILTIN_GRAFT_START, end=hc.BUILTIN_GRAFT_END,
                                import_line="x", tool_names=["y"]) == out


def test_native_tool_template_is_the_vendored_cli_handlebars_file():
    assert hc.NATIVE_TOOL_TEMPLATE.name == "strands_base_main.py.hbs"
    assert "harness_runtime" in hc.NATIVE_TOOL_TEMPLATE.parts


@pytest.mark.parametrize("template", [MIN_TEMPLATE, REAL_MAIN_PY], ids=["fixture", "real"])
def test_config_bundle_graft_leaves_no_unrendered_placeholder_in_the_constant_body(template):
    """Lead ruling (Task 1 review, step 0): `graft_config_bundle` redirects the
    *usage* site (`system_prompt=DEFAULT_SYSTEM_PROMPT` → `resolve_system_prompt()`)
    but the vendored template still bakes the constant's own body as the literal
    placeholder `__LAUNCHPAD_REFERENCE_PROMPT__` — left alone, that placeholder would
    ship as the bundle's fallback prompt in every render. `render_skill_sources` (A6)
    resolves the sibling `__LAUNCHPAD_SKILLS__` placeholder — rendering it too lets
    this assert the FULL renderer chain Task 3 runs leaves no `__LAUNCHPAD_` substring
    anywhere, override given or not. Do NOT narrow or exempt the scan."""
    out = hc.render_skill_sources(template, [])
    out = hc.graft_config_bundle(out, default_system_prompt="You are the Launchpad assistant.")
    compile(out, "main.py", "exec")
    assert "__LAUNCHPAD_" not in out
    assert 'DEFAULT_SYSTEM_PROMPT = """You are the Launchpad assistant."""' in out

    out_default = hc.render_skill_sources(template, [])
    out_default = hc.graft_config_bundle(out_default)
    compile(out_default, "main.py", "exec")
    assert "__LAUNCHPAD_" not in out_default


def _baked_prompt(main_py: str) -> str:
    tree = ast.parse(main_py)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "DEFAULT_SYSTEM_PROMPT" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("DEFAULT_SYSTEM_PROMPT constant missing")


@pytest.mark.parametrize("prompt", [
    'Say "hi"',                 # trailing quote
    '"""""x',                   # five consecutive quotes
    'ends with backslash-quote \\"',
    "trailing backslash \\",
    'mixed \\"""\\ """ end"',
], ids=["trailing-quote", "five-quotes", "backslash-quote", "backslash", "mixed"])
def test_prompt_constant_body_round_trips_awkward_quoting(prompt):
    """A1 (fix round 1, F2): uniform escaping — the baked constant always evaluates back
    to the exact prompt, compiles, and survives a re-graft with a new prompt."""
    out = hc.render_skill_sources(MIN_TEMPLATE, [])
    out = hc.graft_config_bundle(out, default_system_prompt=prompt)
    compile(out, "main.py", "exec")
    assert _baked_prompt(out) == prompt
    assert len(hc._PROMPT_CONST_RE.findall(out)) == 1  # one module-level constant
    again = hc.graft_config_bundle(out, default_system_prompt="second " + prompt)
    compile(again, "main.py", "exec")
    assert _baked_prompt(again) == "second " + prompt
    assert len(hc._PROMPT_CONST_RE.findall(again)) == 1


# ─── memory grafts (N8; memory-runtime.md §6 steps 2–3) ───────────────────────
REAL_TEMPLATE_MAIN = (
    Path(__file__).resolve().parents[1] / "app" / "templates" / "harness_runtime" / "main.py.tmpl"
).read_text()
FIXTURE_TEMPLATE_MAIN = (FIXTURES / "harness_runtime_min" / "main.py.tmpl").read_text()


def _agent_hooks_elements(main_py: str) -> list[ast.expr]:
    tree = ast.parse(main_py)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Agent"):
            (hooks,) = [kw.value for kw in node.keywords if kw.arg == "hooks"]
            assert isinstance(hooks, ast.List)
            return hooks.elts
    raise AssertionError("no Agent(...) call")


@pytest.mark.parametrize("source", [REAL_TEMPLATE_MAIN, FIXTURE_TEMPLATE_MAIN, MAIN_PY],
                         ids=["real-template", "fixture-template", "export"])
def test_memory_hooks_graft_appends_to_the_execution_limits_list(source):
    grafted = hc.graft_memory_hooks(source)
    compile(grafted, "main.py", "exec")
    elts = _agent_hooks_elements(grafted)
    assert len(elts) == 2  # append, never replace: the ExecutionLimitsHook stays first
    assert ast.unparse(elts[0]).startswith("ExecutionLimitsHook(")
    assert ast.unparse(elts[1]) == "*make_memory_hooks(user_id, session_id)"
    assert hc.MEMORY_HOOKS_MARK in grafted
    assert hc.graft_memory_hooks(grafted) == grafted  # idempotent
    # the A8 loop-bounds anchors survive the graft
    assert hc.LOOP_ITER_RE.search(grafted) and hc.LOOP_HOOK_TIMEOUT_RE.search(grafted)


def test_memory_hooks_graft_names_its_anchor_on_drift():
    with pytest.raises(hc.ConversionError, match=r"\(A11\)"):
        hc.graft_memory_hooks(REAL_TEMPLATE_MAIN.replace("hooks=[", "hooks=list(["))
    # a factory whose parameters are not (session_id, user_id, …) has no actor to scope to
    with pytest.raises(hc.ConversionError, match=r"\(A11\)"):
        hc.graft_memory_hooks(REAL_TEMPLATE_MAIN.replace(
            "def get_or_create_agent(session_id, user_id, skill_plugins=None):",
            "def get_or_create_agent(sid, uid, skill_plugins=None):",
        ))


def test_memory_import_graft_degrades_to_no_memory_when_the_library_is_unimportable():
    grafted = hc.graft_memory_import(REAL_TEMPLATE_MAIN)
    assert hc.MEMORY_GRAFT_START in grafted and hc.MEMORY_GRAFT_END in grafted
    assert "from launchpad_memory import make_memory_hooks, recall_context" in grafted
    assert grafted.index(hc.MEMORY_GRAFT_START) > grafted.index(
        "from memory.session import get_memory_session_manager"
    )
    assert hc.graft_memory_import(grafted) == grafted
    with pytest.raises(hc.ConversionError, match=r"\(A10\)"):
        hc.graft_memory_import(REAL_TEMPLATE_MAIN.replace(
            "from memory.session import get_memory_session_manager", "import memory.session"))
    # the fallback definitions are inert
    block = grafted[grafted.index(hc.MEMORY_GRAFT_START): grafted.index(hc.MEMORY_GRAFT_END)]
    namespace: dict = {}
    exec(block.replace("from launchpad_memory import", "from launchpad_memory_absent import"),
         namespace)  # noqa: S102 — our own graft text
    assert namespace["make_memory_hooks"]("a", "s") == []
    assert namespace["recall_context"]("a", "q") == ""
    assert namespace["_launchpad_recall_query"]("hello") == "hello"
    assert namespace["_launchpad_recall_query"](
        [{"role": "assistant", "content": [{"text": "hi"}]},
         {"role": "user", "content": [{"text": "my name"}, {"text": "is River"}]}]
    ) == "my name\nis River"
    assert namespace["_launchpad_recall_query"](
        [{"role": "user", "content": [{"toolResult": {}}]}]
    ) == ""
    assert namespace["_launchpad_recall_query"]({"prompt": "x"}) == ""


@pytest.mark.parametrize("source", [REAL_TEMPLATE_MAIN, FIXTURE_TEMPLATE_MAIN],
                         ids=["real-template", "fixture-template"])
def test_memory_recall_graft_prepends_recall_before_the_turn_streams(source):
    main = hc.graft_config_bundle(source, default_system_prompt="p")
    main = hc.graft_runtime_attachments(main)
    main = hc.graft_actor_from_payload(main)
    grafted = hc.graft_memory_recall(main)
    compile(grafted, "main.py", "exec")
    assert hc.MEMORY_RECALL_START in grafted and hc.MEMORY_RECALL_END in grafted
    assert hc.graft_memory_recall(grafted) == grafted
    tree = ast.parse(grafted)
    (invoke,) = [
        n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "invoke"
    ]
    unparsed = [ast.unparse(stmt) for stmt in invoke.body]
    i_agent = next(
        i for i, s in enumerate(unparsed) if s.startswith("agent = get_or_create_agent(")
    )
    i_prompt = unparsed.index("prompt = _extract_prompt(payload)")
    i_query = unparsed.index("_launchpad_query = _launchpad_recall_query(prompt)")
    i_known = next(i for i, s in enumerate(unparsed) if s.startswith("_launchpad_known = "))
    # A12 fix: the reset keys on the USER turn (_launchpad_query), not on recall
    # presence (_launchpad_known) — see test_memory_recall_graft_resets_stale_context_*
    # below for why (the Harness caches one Agent per session/actor).
    i_set = next(i for i, s in enumerate(unparsed) if s.startswith("if _launchpad_query:"))
    i_attach = next(
        i for i, s in enumerate(unparsed) if s.startswith("if payload.get('attachments'):")
    )
    stream_line = next(n.lineno for n in ast.walk(invoke)
                       if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and n.func.attr == "stream_async")
    # actor + agent exist → raw prompt extracted → recall → attachments adapt → stream
    assert i_agent < i_prompt < i_query < i_known < i_set < i_attach
    assert invoke.body[i_set].lineno < stream_line
    assert ast.unparse(invoke.body[i_known]) == (
        "_launchpad_known = recall_context(user_id, _launchpad_query) if _launchpad_query else ''"
    )
    assert "Known user context:" in ast.unparse(invoke.body[i_set])
    assert "resolve_system_prompt()" in ast.unparse(invoke.body[i_set])
    assert "_launchpad_known" in ast.unparse(invoke.body[i_set])


def _exec_recall_turn(grafted: str, *, prompt, agent_system_prompt: str, known_text: str,
                       user_id: str = "actor-1") -> str:
    """Simulate one invoke() turn of the grafted A12 recall block against a stand-in
    for the Harness's cached Agent (`agent_factory()`'s `cache[key]` keeps the SAME
    Agent object across turns — unlike the per-turn strands_agent HTTP runtime).
    Execs the real graft text (import block for `_launchpad_recall_query` + the
    recall block itself) with `recall_context`/`resolve_system_prompt` stubbed, and
    returns the resulting `agent.system_prompt`."""
    import textwrap
    from types import SimpleNamespace

    import_block = grafted[
        grafted.index(hc.MEMORY_GRAFT_START): grafted.index(hc.MEMORY_GRAFT_END)
        + len(hc.MEMORY_GRAFT_END)
    ]
    namespace: dict = {}
    exec(import_block, namespace)  # noqa: S102 — our own graft text
    namespace["recall_context"] = lambda actor, query: known_text if query else ""
    namespace["resolve_system_prompt"] = lambda: "BASE"

    start = grafted.index(hc.MEMORY_RECALL_START)
    end = grafted.index(hc.MEMORY_RECALL_END) + len(hc.MEMORY_RECALL_END)
    block = grafted[start:end]
    code = textwrap.dedent(
        "\n".join(line for line in block.splitlines() if not line.strip().startswith("#"))
    )
    agent = SimpleNamespace(system_prompt=agent_system_prompt)
    namespace.update({"prompt": prompt, "user_id": user_id, "agent": agent})
    exec(code, namespace)  # noqa: S102 — our own graft text
    return agent.system_prompt


@pytest.mark.parametrize("source", [REAL_TEMPLATE_MAIN, FIXTURE_TEMPLATE_MAIN],
                         ids=["real-template", "fixture-template"])
def test_memory_recall_graft_resets_stale_context_on_a_recall_empty_user_turn(source):
    """A12 fix: the self-built Harness caches one Agent per session/actor (A11), so a
    `Known user context:` block left on `agent.system_prompt` by a prior turn must be
    cleared on the NEXT user turn even when that turn's own recall is empty — the
    reset keys on the user turn (_launchpad_query), not on recall presence
    (_launchpad_known). A tool-result continuation turn (_launchpad_query == "") must
    leave the cached Agent's system_prompt completely untouched."""
    main = hc.graft_config_bundle(source, default_system_prompt="p")
    main = hc.graft_runtime_attachments(main)
    main = hc.graft_actor_from_payload(main)
    main = hc.graft_memory_import(main)
    grafted = hc.graft_memory_recall(main)

    # Turn 1: user turn, recall present → context grafted on.
    sp = _exec_recall_turn(
        grafted, prompt="my name is River", agent_system_prompt="BASE",
        known_text="User's name is River",
    )
    assert sp == "BASE\n\nKnown user context:\nUser's name is River"

    # Turn 2: NEXT user turn on the SAME cached agent, recall now empty → must reset
    # back to the base prompt, not inherit turn 1's stale context.
    sp = _exec_recall_turn(grafted, prompt="what's 2+2?", agent_system_prompt=sp, known_text="")
    assert sp == "BASE"

    # Turn 3: tool-result continuation (_launchpad_query == "") → system_prompt must
    # be left exactly as the loop set it, untouched by the recall graft.
    sp = _exec_recall_turn(
        grafted,
        prompt=[{"role": "user", "content": [{"toolResult": {}}]}],
        agent_system_prompt="TAMPERED-BY-LOOP",
        known_text="should not matter",
    )
    assert sp == "TAMPERED-BY-LOOP"


def test_memory_recall_graft_requires_the_bundle_graft_and_names_its_anchor():
    with pytest.raises(hc.ConversionError, match=r"\(A12\)"):
        hc.graft_memory_recall(REAL_TEMPLATE_MAIN)  # resolve_system_prompt() not grafted yet
    main = hc.graft_config_bundle(REAL_TEMPLATE_MAIN, default_system_prompt="p")
    with pytest.raises(hc.ConversionError, match=r"\(A12\)"):
        hc.graft_memory_recall(main.replace("prompt = _extract_prompt(payload)",
                                            "prompt = payload.get('prompt', '')"))
    with pytest.raises(hc.ConversionError, match=r"\(A12\)"):
        hc.graft_memory_recall(main.replace(
            "agent = get_or_create_agent(session_id, user_id, _skill_plugins)",
            "agent = get_or_create_agent(_skill_plugins)"))


def test_graft_memory_applies_all_three_once():
    main = hc.graft_config_bundle(REAL_TEMPLATE_MAIN, default_system_prompt="p")
    main = hc.graft_actor_from_payload(main)
    grafted = hc.graft_memory(main)
    compile(grafted, "main.py", "exec")
    for mark in (hc.MEMORY_GRAFT_START, hc.MEMORY_HOOKS_MARK, hc.MEMORY_RECALL_START):
        assert grafted.count(mark) == 1
    assert hc.graft_memory(grafted) == grafted
