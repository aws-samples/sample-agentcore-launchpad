"""Harness → runtime conversion: graft anchors, env discovery, requirements
flattening, bundle packaging, and the /convert endpoint contract."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.routers.agents as agents_router
import app.services.harness_convert as hc
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.deployer.zip_runtime import (
    _method_requirements,
    platform_requirements,
    write_bundle_files,
)
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec

FIXTURES = Path(__file__).parent / "fixtures"
MAIN_PY = (FIXTURES / "harness_export_main.py").read_text()
PYPROJECT = (FIXTURES / "harness_export_pyproject.toml").read_text()
MCP_CLIENT_PY = (FIXTURES / "harness_export_mcp_client.py").read_text()


@pytest.fixture(autouse=True)
def stub_pin_resolution(monkeypatch):
    """Conversion resolves the source Harness's dependency ranges to pins, which
    shells out to `uv pip compile` and therefore needs the network (or a warm uv
    cache). Stub it so this suite stays hermetic — `resolve_pins` itself is
    covered against a stub runner in test_requirements_pinning.py.

    Records the `platform` argument in `RESOLVED_AGAINST`: the pins are only
    lockable if the conversion hands over the deploy's full platform contribution,
    so that argument is part of the contract and needs asserting.
    """
    RESOLVED_AGAINST.clear()

    def fake_resolve(entries, platform):
        RESOLVED_AGAINST.append(list(platform))
        out = []
        for entry in entries:
            if "==" in entry and "*" not in entry:
                out.append(entry)
                continue
            name, _, _ = entry.partition(" ")
            name = name.split(">")[0].split("<")[0].split("~")[0].split("=")[0].strip()
            out.append(f"{name}==9.9.9")
        return out

    monkeypatch.setattr(hc, "resolve_pins", fake_resolve)


RESOLVED_AGAINST: list[list[str]] = []


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


def test_graft_supports_skill_plugin_agent_factory_call():
    skill_export = MAIN_PY.replace(
        "agent = get_or_create_agent(session_id, user_id)",
        "agent = get_or_create_agent(session_id, user_id, _skill_plugins)",
    )

    grafted = hc.graft_config_bundle(skill_export)

    assignment = "agent = get_or_create_agent(session_id, user_id, _skill_plugins)"
    assert f"{assignment}\n    _launchpad_apply_tool_descriptions(agent)" in grafted


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


# ─── env discovery ───────────────────────────────────────────────────────────
def test_discover_env_leaves_an_unresolvable_gateway_key_unset(monkeypatch):
    """No shared Gateway in the bootstrap config ⇒ the key stays unset and the
    exported client skips the gateway with a warning."""
    monkeypatch.setattr(
        hc, "get_settings",
        lambda: SimpleNamespace(resources={"memory_id": "launchpad_memory-XYZ"}),
    )
    files = {
        "memory/session.py":
            'MEMORY_ID = os.getenv("MEMORY_MEMORY_LAUNCHPAD_MEMORY_HURAGN3ENF_ID")\n'
            'REGION = os.getenv("AWS_REGION")',
        "mcp_client/client.py":
            'url = os.environ.get("GATEWAY_GATEWAY_LAUNCHPAD_KB_GW_PMYQ7MCHUM_URL")',
    }
    env = hc.discover_env(files)
    assert env["MEMORY_MEMORY_LAUNCHPAD_MEMORY_HURAGN3ENF_ID"] == "launchpad_memory-XYZ"
    assert env["GATEWAY_GATEWAY_LAUNCHPAD_KB_GW_PMYQ7MCHUM_URL"] is None
    assert "AWS_REGION" not in env  # runtime-provided


def test_discover_env_wires_the_shared_gateway_but_not_the_kb_gateway(monkeypatch):
    """The v1 caveat is removed, not reworded: the shared Gateway URL is wired.

    The KB gateway's own key stays unset on purpose — conversion replaces it with
    grafted direct-retrieval tools, so wiring it too would give the runtime two
    duplicate retrieval surfaces.
    """
    monkeypatch.setattr(
        hc, "get_settings",
        lambda: SimpleNamespace(resources={
            "gateway_id": "launchpad-gw-em0yuqmmdp",
            "gateway_url": "https://launchpad-gw-em0yuqmmdp.gateway.example/mcp",
            "kb_gateway_id": "launchpad-kb-gw-pmyq7mchum",
            "kb_gateway_url": "https://launchpad-kb-gw-pmyq7mchum.gateway.example/mcp",
        }),
    )
    files = {
        "mcp_client/client.py":
            'a = os.environ.get("GATEWAY_GATEWAY_LAUNCHPAD_GW_EM0YUQMMDP_URL")\n'
            'b = os.environ.get("GATEWAY_GATEWAY_LAUNCHPAD_KB_GW_PMYQ7MCHUM_URL")',
    }
    env = hc.discover_env(files)
    assert env["GATEWAY_GATEWAY_LAUNCHPAD_GW_EM0YUQMMDP_URL"] == (
        "https://launchpad-gw-em0yuqmmdp.gateway.example/mcp"
    )
    assert env["GATEWAY_GATEWAY_LAUNCHPAD_KB_GW_PMYQ7MCHUM_URL"] is None


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


def test_flatten_requirements_dedupes_against_extras_lists_too():
    """The platform names `openai` only in MANTLE_EXTRA_REQUIREMENTS, never in the
    template base list. Handing over only the base list left the Harness's own
    `openai` in the spec, so the package stage received the project twice —
    the tell that the conversion was working from a partial platform view."""
    files = {"pyproject.toml": PYPROJECT.replace(
        '    "mcp >= 1.19.0",', '    "mcp >= 1.19.0",\n    "openai >= 1.0",'
    )}
    platform = platform_requirements("zip_runtime", "mantle")
    assert any(p.startswith("openai") for p in platform)  # guards the premise

    extras = hc.flatten_requirements(files, platform)
    names = {e.split(">=")[0].split("[")[0].strip().lower() for e in extras}
    assert "openai" not in names
    assert "mcp" in names  # not named by any platform list — must survive


# ─── spec + packaging ────────────────────────────────────────────────────────
def _source_agent():
    return SimpleNamespace(
        id="src1", name="aurora-support",
        arn="arn:aws:bedrock-agentcore:us-west-2:1:harness/aurora_support-X",
        spec={"system_prompt": "You are the Aurora Deck support assistant.",
              "memory": {"short_term": True, "long_term": False}},
    )


def test_build_conversion_spec_shape(monkeypatch):
    monkeypatch.setattr(
        hc, "get_settings",
        lambda: SimpleNamespace(resources={"memory_id": "mem-1"}),
    )
    files = {"main.py": MAIN_PY, "pyproject.toml": PYPROJECT,
             "mcp_client/client.py":
                 'url = os.environ.get("GATEWAY_GATEWAY_X_URL")'}
    spec = hc.build_conversion_spec(
        _source_agent(), files, ["bedrock-agentcore==1.17.*"], "aurora-support-rt",
    )
    assert spec.method == "zip_runtime"
    assert spec.code_bundle and "main.py" in spec.code_bundle
    assert "pyproject.toml" not in spec.code_bundle  # not runtime source
    assert "resolve_system_prompt()" in spec.code_bundle["main.py"]
    assert spec.source_harness["agent_name"] == "aurora-support"
    assert spec.conversion_notes["system_prompt"].startswith("wired")
    assert spec.conversion_notes["kb_gateway"].startswith("not wired")
    # unresolvable against this stubbed config, so still absent from env
    assert "GATEWAY_GATEWAY_X_URL" not in spec.env


def test_build_conversion_spec_materializes_direct_kb_support(monkeypatch):
    monkeypatch.setattr(
        hc, "get_settings",
        lambda: SimpleNamespace(resources={"memory_id": "mem-1"}),
    )
    source = _source_agent()
    source.spec = {
        **source.spec,
        "knowledge_bases": [
            {
                "kb_id": "KB111",
                "name": "fund-docs",
                "description": "fund product PDFs",
            },
            {"kb_id": "KB222", "name": "faq", "description": ""},
        ],
        "tool_description_overrides": {"kb_search": "promoted fast search"},
    }
    files = {
        "main.py": MAIN_PY,
        "pyproject.toml": PYPROJECT,
        "mcp_client/client.py": 'url = os.environ.get("GATEWAY_GATEWAY_X_URL")',
    }

    spec = hc.build_conversion_spec(
        source, files, ["bedrock-agentcore==1.17.*"], "aurora-support-rt"
    )

    assert [kb.kb_id for kb in spec.knowledge_bases] == ["KB111", "KB222"]
    assert spec.code_bundle and "launchpad_kb_tools.py" in spec.code_bundle
    kb_source = spec.code_bundle["launchpad_kb_tools.py"]
    assert "def kb_search(" in kb_source and "def kb_deep_search(" in kb_source
    assert "MOUNTED_KBS" in kb_source and "KB111" in kb_source
    main_py = spec.code_bundle["main.py"]
    assert "from launchpad_kb_tools import kb_deep_search, kb_search" in main_py
    assert "tools.extend([kb_search, kb_deep_search])" in main_py
    assert "`kb_search`" in spec.system_prompt
    assert "`kb_deep_search`" in spec.system_prompt
    assert "___Retrieve" not in spec.system_prompt
    assert spec.tool_description_overrides["kb_search"] == "promoted fast search"
    assert spec.tool_description_overrides["kb_deep_search"].startswith("Deep-search")
    assert "_LAUNCHPAD_DEFAULT_SYSTEM_PROMPT" in main_py
    assert "promoted fast search" in main_py
    assert "GATEWAY_GATEWAY_X_URL" not in spec.env
    assert spec.conversion_notes["knowledge_bases"].startswith("wired (direct")


def test_build_conversion_spec_without_kbs_keeps_existing_bundle_shape(monkeypatch):
    monkeypatch.setattr(
        hc, "get_settings",
        lambda: SimpleNamespace(resources={"memory_id": "mem-1"}),
    )
    files = {"main.py": MAIN_PY, "pyproject.toml": PYPROJECT}
    spec = hc.build_conversion_spec(
        _source_agent(), files, ["bedrock-agentcore==1.17.*"], "aurora-support-rt"
    )
    expected = hc.graft_config_bundle(
        MAIN_PY,
        default_system_prompt=_source_agent().spec["system_prompt"],
        tool_description_overrides=None,
    )
    assert spec.code_bundle == {"main.py": expected}
    assert spec.knowledge_bases == []
    assert hc.KB_GRAFT_START not in spec.code_bundle["main.py"]


def test_build_conversion_spec_carries_model_source(monkeypatch):
    """model_source rides along with model_id. A source harness stored before the
    field existed converts to "bedrock" (Converse), never Mantle."""
    monkeypatch.setattr(
        hc, "get_settings",
        lambda: SimpleNamespace(resources={"memory_id": "mem-1"}),
    )
    files = {"main.py": MAIN_PY, "pyproject.toml": PYPROJECT}
    base = ["bedrock-agentcore==1.17.*"]

    legacy = hc.build_conversion_spec(_source_agent(), files, base, "aurora-support-rt")
    assert legacy.model_source == "bedrock"

    mantle_source = _source_agent()
    mantle_source.spec = {**mantle_source.spec,
                          "model_id": "openai.gpt-5.6-sol", "model_source": "mantle"}
    converted = hc.build_conversion_spec(mantle_source, files, base, "aurora-support-rt")
    assert converted.model_id == "openai.gpt-5.6-sol"
    assert converted.model_source == "mantle"
    # The CLI export builds its own model (model/load.py), not the strands
    # template's build_model(), so what the conversion owes a Mantle harness is
    # the openai extra — flatten_requirements drops it as a base-pin name.
    from app.deployer.zip_runtime import _method_requirements

    assert any("[openai]" in r for r in _method_requirements(converted))
    assert not any("[openai]" in r for r in _method_requirements(legacy))


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


def test_conversion_carries_skills_so_the_exec_role_grants_s3_read(monkeypatch):
    """`agent_iam` gates the skill S3 statement on `spec.skills`. Dropping the
    field made the deploy report success and the runtime fail at INVOKE with
    `Failed to resolve S3 skill … AccessDenied`, because the exported code fetches
    the prefixes it baked in."""
    monkeypatch.setattr(
        hc, "get_settings",
        lambda: SimpleNamespace(resources={"memory_id": "mem-1"}),
    )
    source = _source_agent()
    source.spec = {**source.spec, "skills": [SKILL_URI]}
    files = {
        "main.py": MAIN_PY + f'\ns3_skill_sources = ["{SKILL_URI}"]\n',
        "pyproject.toml": PYPROJECT,
    }

    spec = hc.build_conversion_spec(
        source, files, ["bedrock-agentcore==1.17.*"], "aurora-support-rt"
    )

    assert spec.skills == [SKILL_URI]
    assert spec.conversion_notes["skills"].startswith("wired")


def test_skills_only_the_exported_code_names_are_still_granted(monkeypatch):
    """The grant must cover what the CODE fetches. A source row that never
    recorded the bundle (or recorded it before the export changed) would
    otherwise authorize the role for the wrong thing."""
    monkeypatch.setattr(
        hc, "get_settings",
        lambda: SimpleNamespace(resources={"memory_id": "mem-1"}),
    )
    source = _source_agent()  # ledger row has NO skills
    files = {
        "main.py": MAIN_PY + f'\ns3_skill_sources = ["{SKILL_URI}"]\n',
        "pyproject.toml": PYPROJECT,
    }

    spec = hc.build_conversion_spec(
        source, files, ["bedrock-agentcore==1.17.*"], "aurora-support-rt"
    )

    assert spec.skills == [SKILL_URI]


def test_non_s3_skill_sources_are_flagged_not_silently_claimed(monkeypatch):
    monkeypatch.setattr(
        hc, "get_settings",
        lambda: SimpleNamespace(resources={"memory_id": "mem-1"}),
    )
    files = {
        "main.py": MAIN_PY
        + '\ngit_skill_sources = ["https://example.invalid/repo.git"]\n',
        "pyproject.toml": PYPROJECT,
    }

    spec = hc.build_conversion_spec(
        _source_agent(), files, ["bedrock-agentcore==1.17.*"], "aurora-support-rt"
    )

    assert spec.skills == []  # no S3 prefix to grant
    assert spec.conversion_notes["skills_non_s3"].startswith("not verified")


def test_a_skill_free_harness_claims_nothing_about_skills(monkeypatch):
    monkeypatch.setattr(
        hc, "get_settings",
        lambda: SimpleNamespace(resources={"memory_id": "mem-1"}),
    )
    spec = hc.build_conversion_spec(
        _source_agent(),
        {"main.py": MAIN_PY, "pyproject.toml": PYPROJECT},
        ["bedrock-agentcore==1.17.*"],
        "aurora-support-rt",
    )
    assert spec.skills == []
    assert "skills" not in spec.conversion_notes
    assert "skills_non_s3" not in spec.conversion_notes


def test_pins_are_resolved_against_the_specs_own_platform_list(monkeypatch):
    """A Mantle harness (the lab-quota-advisor shape) must resolve its pins against
    the Mantle extras too. Resolving against the base list alone is what produced
    `mcp==2.0.0` — un-lockable, because strands-agents caps `mcp<2.0.0`."""
    monkeypatch.setattr(
        hc, "get_settings",
        lambda: SimpleNamespace(resources={"memory_id": "mem-1"}),
    )
    files = {"main.py": MAIN_PY, "pyproject.toml": PYPROJECT}
    mantle_source = _source_agent()
    mantle_source.spec = {**mantle_source.spec, "model_source": "mantle"}

    platform = platform_requirements(*hc.conversion_platform_inputs(mantle_source))
    hc.build_conversion_spec(mantle_source, files, platform, "aurora-support-rt")

    assert RESOLVED_AGAINST, "resolve_pins was never called"
    resolved_against = RESOLVED_AGAINST[-1]
    assert "strands-agents[openai]>=1.47,<2" in resolved_against
    assert "openai>=2,<3" in resolved_against
    assert any(r.startswith("strands-agents[otel]") for r in resolved_against)


def test_the_pin_platform_matches_what_the_package_stage_will_prepend(monkeypatch):
    """Drift guard. The router derives the platform list *before* the spec exists;
    if that derivation disagrees with the spec actually built, pins are resolved
    against a graph the deploy never uses — silently reintroducing this whole bug.
    """
    monkeypatch.setattr(
        hc, "get_settings",
        lambda: SimpleNamespace(resources={"memory_id": "mem-1"}),
    )
    files = {"main.py": MAIN_PY, "pyproject.toml": PYPROJECT}
    for model_source in ("bedrock", "mantle"):
        source = _source_agent()
        source.spec = {**source.spec, "model_source": model_source}

        # what the router computes up front …
        platform = platform_requirements(*hc.conversion_platform_inputs(source))
        spec = hc.build_conversion_spec(source, files, platform, "aurora-support-rt")

        # … must equal what the package stage prepends to the resulting spec
        assert platform == platform_requirements(
            spec.method, spec.model_source, spec.protocol
        )
        assert _method_requirements(spec) == platform + spec.requirements


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


# ─── endpoint contract ──────────────────────────────────────────────────────
def _mk_agent(**kw):
    db = SessionLocal()
    agent = Agent(**{"name": "h-src", "method": "harness", "status": "active",
                     "arn": "arn:h", "spec": {"system_prompt": "sp"}, **kw})
    db.add(agent)
    db.commit()
    db.refresh(agent)
    db.close()
    return agent


def test_convert_rejects_non_harness_and_inactive(client):
    runtime_agent = _mk_agent(name="rt-src", method="zip_runtime")
    res = client.post(f"/api/agents/{runtime_agent.id}/convert")
    assert res.status_code == 400
    assert res.json()["code"] == "agent.convert_unsupported"

    inactive = _mk_agent(name="h-off", status="stopped")
    res = client.post(f"/api/agents/{inactive.id}/convert")
    assert res.status_code == 400


def test_convert_happy_path_and_in_flight_guard(client, monkeypatch):
    source = _mk_agent(name="aurora-support")
    monkeypatch.setattr(
        hc, "export_harness",
        lambda arn: {"main.py": MAIN_PY, "pyproject.toml": PYPROJECT},
    )
    monkeypatch.setattr(
        hc, "get_settings",
        lambda: SimpleNamespace(resources={"memory_id": "mem-1"}),
    )
    started: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async",
                        lambda job_id: started.append(job_id))

    res = client.post(f"/api/agents/{source.id}/convert")
    assert res.status_code == 202
    body = res.json()["agent"]
    assert body["name"] == "aurora-support-rt"
    assert body["method"] == "zip_runtime"
    assert body["status"] == "deploying"
    assert body["spec"]["source_harness"]["agent_id"] == source.id
    assert "resolve_system_prompt()" in body["spec"]["code_bundle"]["main.py"]
    assert started, "deploy job must be kicked"

    # same source, conversion still deploying → 409
    res = client.post(f"/api/agents/{source.id}/convert")
    assert res.status_code == 409
    assert res.json()["code"] == "agent.convert_in_flight"


def test_convert_happy_path_persists_direct_kb_bundle(client, monkeypatch):
    source = _mk_agent(
        name="kb-support",
        spec={
            "system_prompt": "Use the product corpus.",
            "knowledge_bases": [
                {"kb_id": "KB111", "name": "Product Docs", "description": "features"}
            ],
        },
    )
    monkeypatch.setattr(
        hc,
        "export_harness",
        lambda arn: {
            "main.py": MAIN_PY,
            "mcp_client/client.py": 'url = os.environ.get("GATEWAY_GATEWAY_X_URL")',
        },
    )
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda job_id: None)

    res = client.post(f"/api/agents/{source.id}/convert")

    assert res.status_code == 202
    spec = res.json()["agent"]["spec"]
    assert spec["knowledge_bases"][0]["kb_id"] == "KB111"
    assert "launchpad_kb_tools.py" in spec["code_bundle"]
    assert "kb_search" in spec["code_bundle"]["main.py"]
    assert "`kb_deep_search`" in spec["system_prompt"]
    assert "GATEWAY_GATEWAY_X_URL" not in spec["env"]


def test_convert_name_dedupe(client, monkeypatch):
    source = _mk_agent(name="hr-assistant")
    _mk_agent(name="hr-assistant-rt", method="zip_runtime")  # name taken (active)
    monkeypatch.setattr(hc, "export_harness",
                        lambda arn: {"main.py": MAIN_PY})
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda job_id: None)
    res = client.post(f"/api/agents/{source.id}/convert")
    assert res.status_code == 202
    assert res.json()["agent"]["name"] == "hr-assistant-rt-2"


def test_convert_graft_failure_is_clean(client, monkeypatch):
    source = _mk_agent(name="h-graftless")
    monkeypatch.setattr(hc, "export_harness",
                        lambda arn: {"main.py": "print('no anchors')"})
    res = client.post(f"/api/agents/{source.id}/convert")
    assert res.status_code == 502
    assert res.json()["code"] == "agent.convert_failed"
    db = SessionLocal()
    leftovers = db.query(Agent).filter(Agent.name.like("h-graftless-rt%")).all()
    db.close()
    assert leftovers == []  # no half-registered row (A2)


def test_convert_missing_managed_cli_uses_app_error_without_row(
    client, tmp_path, monkeypatch
):
    source = _mk_agent(name="h-cli-missing")
    monkeypatch.setattr(hc, "MANAGED_AGENTCORE_CLI", tmp_path / "missing")
    monkeypatch.setattr(hc, "_SCRATCH_DIR", tmp_path / "harness-export")

    res = client.post(f"/api/agents/{source.id}/convert")

    assert res.status_code == 502
    assert res.json()["code"] == "agent.convert_cli_missing"
    assert "make bootstrap" in res.json()["message"]
    db = SessionLocal()
    leftovers = db.query(Agent).filter(Agent.name.like("h-cli-missing-rt%")).all()
    db.close()
    assert leftovers == []


def test_convert_direct_kb_anchor_failure_is_clean(client, monkeypatch):
    source = _mk_agent(
        name="h-kb-graftless",
        spec={
            "system_prompt": "Use the corpus.",
            "knowledge_bases": [{"kb_id": "KB111", "name": "Docs"}],
        },
    )
    monkeypatch.setattr(
        hc,
        "export_harness",
        lambda arn: {"main.py": MAIN_PY.replace("tools = []", "tools = [shell]")},
    )
    res = client.post(f"/api/agents/{source.id}/convert")
    assert res.status_code == 502
    assert "direct KB graft anchor missing" in res.json()["message"]
    db = SessionLocal()
    leftovers = db.query(Agent).filter(Agent.name.like("h-kb-graftless-rt%")).all()
    db.close()
    assert leftovers == []


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


def test_last_json_skips_update_notice():
    out = json.dumps({"success": True, "agentPath": "/x"}) + \
        "\n\nUpdate available: 0.21.1 → 0.24.0\n"
    # update notice AFTER the json — reversed scan still finds the object
    assert hc._last_json(out)["success"] is True


def test_last_json_rejects_non_object_json():
    with pytest.raises(hc.ConversionError, match="returned no JSON"):
        hc._last_json('["not", "a", "result"]\n')


# ─── managed CLI ────────────────────────────────────────────────────────────
def _managed_cli(tmp_path: Path) -> Path:
    cli = tmp_path / "agentcore-cli" / "node_modules" / ".bin" / "agentcore"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    return cli


def test_managed_cli_missing_uses_existing_error_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "MANAGED_AGENTCORE_CLI", tmp_path / "missing")

    with pytest.raises(AppError) as exc_info:
        hc.resolve_agentcore_cli()

    assert exc_info.value.code == "agent.convert_cli_missing"
    assert exc_info.value.status_code == 502
    assert "make bootstrap" in exc_info.value.message


def test_managed_cli_timeout_uses_conversion_error(tmp_path, monkeypatch):
    cli = _managed_cli(tmp_path)
    monkeypatch.setattr(hc, "MANAGED_AGENTCORE_CLI", cli)

    def timed_out(cmd, cwd):
        raise hc.subprocess.TimeoutExpired(cmd=cmd, timeout=hc.EXPORT_TIMEOUT_S)

    monkeypatch.setattr(hc, "_run", timed_out)

    with pytest.raises(hc.ConversionError, match="timed out after 120 seconds"):
        hc._run_agentcore(["export"], cwd=tmp_path)


def test_scratch_create_uses_managed_cli_path(tmp_path, monkeypatch):
    cli = _managed_cli(tmp_path)
    scratch = tmp_path / "harness-export"
    project = scratch / hc.SCRATCH_PROJECT
    calls = []
    monkeypatch.setattr(hc, "MANAGED_AGENTCORE_CLI", cli)
    monkeypatch.setattr(hc, "_SCRATCH_DIR", scratch)

    def fake_run(cmd, cwd):
        calls.append((cmd, cwd))
        return SimpleNamespace(
            stdout=json.dumps({"success": True, "projectPath": str(project)})
        )

    monkeypatch.setattr(hc, "_run", fake_run)

    assert hc.ensure_scratch_project() == project
    assert calls == [
        (
            [
                str(cli),
                "create",
                "--project-name",
                hc.SCRATCH_PROJECT,
                "--no-agent",
                "--json",
            ],
            scratch,
        )
    ]


def test_harness_export_uses_managed_cli_path(tmp_path, monkeypatch):
    cli = _managed_cli(tmp_path)
    scratch = tmp_path / "harness-export"
    project = scratch / hc.SCRATCH_PROJECT
    project.mkdir(parents=True)
    exported = tmp_path / "exported"
    exported.mkdir()
    (exported / "main.py").write_text("print('exported')\n")
    calls = []
    monkeypatch.setattr(hc, "MANAGED_AGENTCORE_CLI", cli)
    monkeypatch.setattr(hc, "_SCRATCH_DIR", scratch)

    def fake_run(cmd, cwd):
        calls.append((cmd, cwd))
        return SimpleNamespace(
            stdout=json.dumps({"success": True, "agentPath": str(exported)})
        )

    monkeypatch.setattr(hc, "_run", fake_run)

    assert hc.export_harness("arn:harness") == {"main.py": "print('exported')\n"}
    assert len(calls) == 1
    cmd, cwd = calls[0]
    assert cwd == project
    target = cmd[cmd.index("--target-agent-name") + 1]
    assert cmd == [
        str(cli),
        "export",
        "harness",
        "--arn",
        "arn:harness",
        "--target-agent-name",
        target,
        "--build",
        "CodeZip",
        "--json",
    ]


def test_harness_export_never_reuses_a_target_agent_name(tmp_path, monkeypatch):
    """The CLI derives `<harnessName>Agent` and REFUSES to overwrite it, so a
    default-named export makes every second conversion of the same harness fail
    with `A runtime agent named "…" already exists`. A per-call name also stops two
    concurrent conversions from sharing an export directory.
    """
    cli = _managed_cli(tmp_path)
    scratch = tmp_path / "harness-export"
    (scratch / hc.SCRATCH_PROJECT).mkdir(parents=True)
    monkeypatch.setattr(hc, "MANAGED_AGENTCORE_CLI", cli)
    monkeypatch.setattr(hc, "_SCRATCH_DIR", scratch)
    targets = []

    def fake_run(cmd, cwd):
        target = cmd[cmd.index("--target-agent-name") + 1]
        targets.append(target)
        exported = tmp_path / "exports" / target
        exported.mkdir(parents=True)
        (exported / "main.py").write_text("print('exported')\n")
        return SimpleNamespace(
            stdout=json.dumps({"success": True, "agentPath": str(exported)})
        )

    monkeypatch.setattr(hc, "_run", fake_run)

    for _ in range(2):
        assert hc.export_harness("arn:harness") == {"main.py": "print('exported')\n"}

    assert len(set(targets)) == 2, "the same target name was reused"
    # read into memory, then gone: the scratch tree is not an artifact of record
    assert not [p for p in (tmp_path / "exports").iterdir()]


def test_build_conversion_spec_carries_gateway_attachments(monkeypatch):
    """Requirement B / R5. Dropping the gateway ToolRef is the same failure shape
    as the dropped spec.skills once was: the deploy reports READY and the runtime
    reaches no tools, because agent_iam gates the AgentCore Identity statements —
    and the invoke chain gates runtimeUserId — on that field being present."""
    monkeypatch.setattr(
        hc, "get_settings",
        lambda: SimpleNamespace(resources={
            "memory_id": "mem-1",
            "gateway_id": "launchpad-gw-em0yuqmmdp",
            "gateway_url": "https://launchpad-gw-em0yuqmmdp.gateway.example/mcp",
        }),
    )
    source = _source_agent()
    source.spec = {
        **source.spec,
        "tools": [
            {"type": "gateway", "name": "hr-database",
             "config": {"gateway_id": "launchpad-gw-em0yuqmmdp"}},
            {"type": "mcp", "name": "deepwiki", "config": {"url": "https://x/mcp"}},
            {"type": "builtin", "name": "code-interpreter"},
        ],
    }
    files = {
        "main.py": MAIN_PY, "pyproject.toml": PYPROJECT,
        "mcp_client/client.py":
            'url = os.environ.get("GATEWAY_GATEWAY_LAUNCHPAD_GW_EM0YUQMMDP_URL")',
    }
    spec = hc.build_conversion_spec(
        source, files, ["bedrock-agentcore==1.17.*"], "aurora-support-rt",
    )
    # gateway + mcp carried (both drive _uses_gateway); builtin is a Harness-only
    # tool type the exported code does not reproduce
    assert [(t.type, t.name) for t in spec.tools] == [
        ("gateway", "hr-database"), ("mcp", "deepwiki"),
    ]
    assert spec.tools[0].config["gateway_id"] == "launchpad-gw-em0yuqmmdp"
    from app.services.agent_iam import _uses_gateway
    assert _uses_gateway(spec) is True
    from app.templates.gateway_support import runtime_user_id
    assert runtime_user_id(spec.model_dump(), "river") == "river"
    # env wired, soft-fail graft applied, and the note says so truthfully
    assert spec.env["GATEWAY_GATEWAY_LAUNCHPAD_GW_EM0YUQMMDP_URL"] == (
        "https://launchpad-gw-em0yuqmmdp.gateway.example/mcp"
    )
    assert hc.GW_SOFTFAIL_START in spec.code_bundle["main.py"]
    assert spec.conversion_notes["gateway"].startswith("wired")
    assert spec.conversion_notes["gateway_tools"].startswith("wired (2 attachment")
    assert "kb_gateway" not in spec.conversion_notes


def test_build_conversion_spec_skips_the_softfail_graft_without_a_gateway_client(
    monkeypatch,
):
    """An export carrying no mcp_client/client.py has no gateway to soft-fail. That
    is benign — it must not fail the conversion and must not graft anything."""
    monkeypatch.setattr(
        hc, "get_settings", lambda: SimpleNamespace(resources={"memory_id": "mem-1"})
    )
    files = {"main.py": MAIN_PY, "pyproject.toml": PYPROJECT}
    spec = hc.build_conversion_spec(
        _source_agent(), files, ["bedrock-agentcore==1.17.*"], "aurora-support-rt",
    )
    assert spec.tools == []
    assert hc.GW_SOFTFAIL_START not in spec.code_bundle["main.py"]
    assert "gateway_tools" not in spec.conversion_notes


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
    return_at = grafted.index(f"return MCPClient({hc.GW_LAZY_TOKEN_MARK}")
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


def test_conversion_applies_both_gateway_grafts(monkeypatch):
    monkeypatch.setattr(
        hc, "get_settings",
        lambda: SimpleNamespace(resources={
            "memory_id": "mem-1",
            "gateway_id": "launchpad-gw-em0yuqmmdp",
            "gateway_url": "https://launchpad-gw-em0yuqmmdp.gateway.example/mcp",
        }),
    )
    source = _source_agent()
    source.spec = {**source.spec,
                   "tools": [{"type": "gateway", "name": "hr-database"}]}
    files = {"main.py": MAIN_PY, "pyproject.toml": PYPROJECT,
             "mcp_client/client.py": MCP_CLIENT_PY}
    spec = hc.build_conversion_spec(
        source, files, ["bedrock-agentcore==1.17.*"], "aurora-support-rt",
    )
    # main.py: the import-time crash is contained
    assert hc.GW_SOFTFAIL_START in spec.code_bundle["main.py"]
    # client.py: the token fetch is deferred into the request
    assert hc.GW_LAZY_TOKEN_MARK in spec.code_bundle["mcp_client/client.py"]
    compile(spec.code_bundle["mcp_client/client.py"], "client.py", "exec")
    compile(spec.code_bundle["main.py"], "main.py", "exec")
