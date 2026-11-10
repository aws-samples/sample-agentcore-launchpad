"""G(N7) item 2: a 方式B spec renders to a deployable bundle; five stages, three shared."""
import ast
import py_compile
import zipfile
from pathlib import Path

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer import harness_local, zip_runtime
from app.deployer.environment import runtime_environment
from app.deployer.pipeline import STAGE_ORDER, StageContext, get_method
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec, KnowledgeBaseRef, ToolRef
from app.services import aws_clients
from app.services import harness_convert as hc
from app.templates.memory_support import render_memory_source

from .conftest import ws_ctx

FIXTURE_TEMPLATE = Path(__file__).parent / "fixtures" / "harness_runtime_min"
GW_ID = "launchpad-gw-a1b2c3d4e5"
RES = {
    "artifacts_bucket": "launchpad-artifacts-test",
    "gateway_id": GW_ID,
    "gateway_url": f"https://{GW_ID}.gateway.bedrock-agentcore.cn-northwest-1.amazonaws.com.cn/mcp",
    "memory_id": "mem-1",
    "execution_role_arn": "arn:aws-cn:iam::111122223333:role/launchpad-agent-execution-role",
}
GATEWAY = ToolRef(
    type="gateway", name="hr-tools",
    config={"record_id": "rec-1", "gateway_id": GW_ID, "tools": ["hr-tools___lookup"]},
)


@pytest.fixture(autouse=True)
def fixture_template(monkeypatch):
    monkeypatch.setattr(harness_local, "TEMPLATE_DIR", FIXTURE_TEMPLATE)


def _spec(**kw):
    kw.setdefault("system_prompt", "You answer HR questions.")
    return AgentSpec(name="hr-helpdesk", method="harness", **kw)


def _ws():
    return ws_ctx(RES, region="cn-northwest-1")


def _compile_all(files: dict[str, str], tmp_path: Path) -> None:
    for rel, text in files.items():
        if rel.endswith(".py"):
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            py_compile.compile(str(path), doraise=True)


def _copy_fixture(dest: Path) -> Path:
    for p in FIXTURE_TEMPLATE.rglob("*"):
        if p.is_file():
            target = dest / p.relative_to(FIXTURE_TEMPLATE)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(p.read_text())
    return dest


def test_harness_registers_five_stages_sharing_the_zip_tail():
    stages = get_method("harness")
    assert list(stages) == STAGE_ORDER
    assert stages["generate"] is harness_local._stage_generate
    assert stages["package"] is harness_local._stage_package
    assert stages["provision"] is zip_runtime._stage_provision
    assert stages["deploy"] is zip_runtime._stage_deploy
    assert stages["register"] is zip_runtime._stage_register


def test_render_bakes_model_prompt_and_loop_bounds(tmp_path):
    bundle = harness_local.render_harness_bundle(
        _spec(max_tokens=8192, max_iterations=12, timeout_seconds=240), _ws())
    main, load = bundle.files["main.py"], bundle.files["model/load.py"]
    assert 'MODEL_ID = "global.zai.glm-5.3"' in load
    assert "api_key=_resolve_bedrock_api_key()" in load
    assert "region_name=MODEL_REGION" in load and "MODEL_MAX_TOKENS = 8192" in load
    assert "def load_model()" in load
    assert "_LAUNCHPAD_DEFAULT_SYSTEM_PROMPT = 'You answer HR questions.'" in main
    assert "system_prompt=resolve_system_prompt()" in main and hc.GRAFT_START in main
    assert "max_iterations=12," in main and "timeout_seconds = 240" in main
    assert "__LAUNCHPAD_" not in main and "__LAUNCHPAD_" not in load
    for rel in ("memory/__init__.py", "model/__init__.py", "skills/__init__.py",
                "hooks/__init__.py", "mcp_client/__init__.py"):
        assert rel in bundle.files
    # the export's own model loader and memory manager are replaced wholesale (A9/A10)
    assert "from model.load import load_model" in main
    assert "from memory.session import get_memory_session_manager" in main
    assert bundle.files["memory/session.py"] == harness_local.MEMORY_SESSION_SHIM
    assert "MCPClient(" not in bundle.files["mcp_client/client.py"]  # inert scaffold kept
    _compile_all(bundle.files, tmp_path)


def test_render_refuses_a_prompt_carrying_the_placeholder_marker():
    # round 16 rev: the schema refuses the marker first (AgentSpec._no_reserved_placeholder,
    # naming the field); the renderer's post-render scan stays for a stored spec predating it
    with pytest.raises(ValueError, match="system_prompt"):
        _spec(system_prompt="see __LAUNCHPAD_X__")
    stored = _spec()
    stored.system_prompt = "see __LAUNCHPAD_X__"
    with pytest.raises(harness_local.HarnessRenderError, match="system prompt"):
        harness_local.render_harness_bundle(stored, _ws())


def test_render_refuses_a_non_harness_spec_and_a_missing_template(tmp_path):
    with pytest.raises(harness_local.HarnessRenderError, match="method"):
        harness_local.render_harness_bundle(
            AgentSpec(name="zip-agent", method="zip_runtime", system_prompt="p"), _ws())
    with pytest.raises(harness_local.HarnessRenderError, match="harness-template.md"):
        harness_local.render_harness_bundle(_spec(), _ws(), template_dir=tmp_path / "absent")


def test_render_wires_native_tools_gateway_skills_and_kbs(tmp_path):
    spec = _spec(native_tools=["shell"], tools=[GATEWAY], skills=["s3://b/skills/x/"],
                 knowledge_bases=[KnowledgeBaseRef(kb_id="KB1", name="hr", description="policies")])
    bundle = harness_local.render_harness_bundle(spec, _ws())
    main, client = bundle.files["main.py"], bundle.files["mcp_client/client.py"]
    assert "def shell(" in main and "def file_operations(" not in main
    assert hc.NATIVE_GRAFT_START in main
    assert hc.GW_SIGV4_TRANSPORT in client and RES["gateway_url"] in client
    assert "tool_filters={'allowed': ['hr-tools___lookup']}" in client
    assert hc.GW_NO_BUNDLE_CLIENT in client  # SigV4 client wrapped in the baggage-free class
    assert hc.GW_SOFTFAIL_START in main
    assert "s3_skill_sources = ['s3://b/skills/x/']" in main
    assert hc.discover_skills(bundle.files)[0] == ["s3://b/skills/x/"]
    assert "launchpad_kb_tools.py" in bundle.files
    assert "tools.extend([kb_search, kb_deep_search])" in main
    assert "Mounted knowledge bases" in main  # kb_prompt_section folded into the default prompt
    assert "'kb_search': 'Search the mounted" in main  # KB tool descriptions are bundle-tunable
    assert bundle.notes["gateway"].startswith("wired")
    assert bundle.notes["skills"].startswith("wired (1")
    # the ARN that produced the client URL is partition-aware (C2)
    assert "arn:aws:" not in client and ".amazonaws.com/" not in client
    _compile_all(bundle.files, tmp_path)


def test_render_refuses_a_gateway_tool_without_any_gateway_id():
    unresolved = GATEWAY.model_copy(update={"config": {"record_id": "rec-1"}})
    with pytest.raises(harness_local.HarnessRenderError, match="gateway_id"):
        harness_local.render_harness_bundle(
            _spec(tools=[unresolved]),
            ws_ctx({"artifacts_bucket": "b"}, region="cn-northwest-1"))


def test_render_uses_the_selected_gateway_without_a_bootstrap_gateway():
    bundle = harness_local.render_harness_bundle(
        _spec(tools=[GATEWAY]), ws_ctx({"artifacts_bucket": "b"}, region="cn-northwest-1"))
    assert RES["gateway_url"] in bundle.files["mcp_client/client.py"]


def _free_names(source: str) -> set[str]:
    """Names a module reads without binding them itself (imports, defs, assignments,
    parameters, loop/with/except targets) — the same guard model/load.py has (R4)."""
    import builtins

    tree = ast.parse(source)
    bound: set[str] = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            bound |= {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
            bound |= {a.arg for a in (args.vararg, args.kwarg) if a is not None}
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound |= {alias.asname or alias.name.split(".")[0] for alias in node.names}
        if isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
    loads = {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }
    return loads - bound


def test_memory_library_is_bundled_unconditionally_beside_the_shim(tmp_path, monkeypatch):
    """N8 item 1: launchpad_memory.py = model-helper import + render_memory_source(), in
    every bundle (the library is a no-op without LAUNCHPAD_MEMORY_ID); never a second copy
    of the model block (C4: one key cache, one credential path)."""
    spec = _spec(memory={"short_term": True, "long_term": True, "memory_id": None})
    bundle = harness_local.render_harness_bundle(spec, _ws())
    shim = bundle.files["memory/session.py"]
    assert "LAUNCHPAD_MEMORY_ID" in shim
    assert "from launchpad_memory import make_session_manager" in shim
    assert 'payload.get("actor_id")' in bundle.files["main.py"]
    assert runtime_environment(spec, RES)["LAUNCHPAD_MEMORY_ID"] == "mem-1"
    lib = bundle.files["launchpad_memory.py"]
    assert lib == harness_local.MEMORY_LIBRARY_HEADER + render_memory_source()
    assert lib.startswith(
        "from model.load import _resolve_bedrock_api_key, MODEL_REGION, BEDROCK_BEARER_ENV\n"
    )
    assert "def build_model" not in lib and "_BEDROCK_API_KEY" not in lib
    assert not any(k.startswith("launchpad_memory/") for k in bundle.files)
    assert bundle.notes["memory"].startswith("wired")
    assert not hasattr(harness_local, "LAUNCHPAD_MEMORY_DIR")
    # unconditional: a spec with memory off still ships the (inert) library
    off = _spec(memory={"short_term": False, "long_term": False})
    off_bundle = harness_local.render_harness_bundle(off, _ws())
    assert off_bundle.notes["memory"].startswith("off (spec)")
    assert off_bundle.files["launchpad_memory.py"] == lib
    assert "LAUNCHPAD_MEMORY_ID" not in runtime_environment(off, RES)
    # standalone: compiles, and reads nothing but model.load's three names + its imports
    path = tmp_path / "launchpad_memory.py"
    path.write_text(lib)
    py_compile.compile(str(path), doraise=True)
    assert _free_names(lib) == set(), sorted(_free_names(lib))
    assert _free_names(render_memory_source()) == {
        "_resolve_bedrock_api_key", "MODEL_REGION", "BEDROCK_BEARER_ENV",
    }
    # the shim itself never raises: no env → None; env but no library → None (logged)
    namespace: dict = {}
    exec(shim, namespace)  # noqa: S102 — our own rendered text
    monkeypatch.delenv("LAUNCHPAD_MEMORY_ID", raising=False)
    assert namespace["get_memory_session_manager"]("s", "a") is None
    monkeypatch.setenv("LAUNCHPAD_MEMORY_ID", "mem-1")
    assert namespace["get_memory_session_manager"]("s", "a") is None


def test_remote_mcp_is_soft_and_unauthenticated_only():
    knowledge = ToolRef(type="mcp", name="aws_knowledge",
                        config={"url": "https://knowledge-mcp.global.api.aws", "auth": "none"})
    bundle = harness_local.render_harness_bundle(_spec(tools=[knowledge]), _ws())
    client = bundle.files["mcp_client/client.py"]
    assert hc.GW_SOFT_CLIENT in client and "https://knowledge-mcp.global.api.aws" in client
    assert hc.GW_SOFTFAIL_START in bundle.files["main.py"]
    assert bundle.notes["remote_mcp"].startswith("wired") and "gateway" not in bundle.notes
    # a wizard-shaped tool (no "auth" key at all, e.g. the live assistant/preset producers)
    # is unauthenticated by absence and renders as a soft client — F1
    wizard = harness_local.render_harness_bundle(
        _spec(tools=[ToolRef(type="mcp", name="x", config={"url": "https://x.example/mcp"})]),
        _ws(),
    )
    assert hc.GW_SOFT_CLIENT in wizard.files["mcp_client/client.py"]
    # round 16: the schema refuses a URL-less mcp tool first (AgentSpec._mcp_tools_need_url);
    # the renderer keeps its own check for a stored spec that predates it
    with pytest.raises(ValueError, match="config.url"):
        _spec(tools=[ToolRef(type="mcp", name="x", config={"auth": "none"})])
    stored = _spec(tools=[ToolRef(type="mcp", name="x", config={"url": "https://x.example/mcp"})])
    stored.tools[0].config = {"auth": "none"}
    with pytest.raises(harness_local.HarnessRenderError, match="url"):
        harness_local.render_harness_bundle(stored, _ws())
    # a non-"none" config.auth (e.g. a stale "bearer" shape) still refuses too
    with pytest.raises(harness_local.HarnessRenderError, match="auth"):
        harness_local.render_harness_bundle(
            _spec(tools=[ToolRef(
                type="mcp", name="x",
                config={"url": "https://x.example/mcp", "auth": "bearer"},
            )]),
            _ws(),
        )


def test_mcp_tool_auth_block_is_refused_before_the_renderer_ever_sees_it():
    """An explicit ``ToolRef.auth`` block on a harness spec never reaches
    ``harness_local``'s own refusal at all: ``AgentSpec._tool_auth_placement``
    already refuses it for every method but zip_runtime/byoc (R9's "can mint no
    bearer" intent, enforced one layer up). This was true before F1 too — the
    ``ref.auth is not None`` disjunct in ``_synthetic_mcp_tools`` is unreachable
    defense-in-depth, not a path F1 changed."""
    with pytest.raises(ValueError, match="rest tools / tool auth"):
        _spec(tools=[ToolRef(
            type="mcp", name="x", config={"url": "https://x.example/mcp"},
            auth={"connection": "team-idp", "kind": "api_key"},
        )])


def test_assistant_proposal_round_trips_through_the_renderer():
    """F1: the live proposal → AgentSpec producer never emits an mcp tool's
    ``config.auth`` key at all (``to_agent_spec`` just carries the catalog's URL
    through) — the renderer must accept that shape, not just the wizard's explicit
    ``"auth": "none"``."""
    from app.assistant import proposal as contract

    from .test_assistant import CATALOG, VALID_PROPOSAL

    content, errors = contract.parse_content(VALID_PROPOSAL)
    assert content is not None and not errors
    spec = contract.to_agent_spec(content, CATALOG)
    mcp_tool = next(t for t in spec.tools if t.type == "mcp")
    assert mcp_tool.auth is None and "auth" not in (mcp_tool.config or {})
    bundle = harness_local.render_harness_bundle(spec, _ws())
    assert hc.GW_SOFT_CLIENT in bundle.files["mcp_client/client.py"]
    assert bundle.notes["remote_mcp"].startswith("wired")


def test_builtin_code_interpreter_renders_a_tool_and_adds_its_requirement(tmp_path):
    bundle = harness_local.render_harness_bundle(
        _spec(tools=[ToolRef(type="builtin", name="code-interpreter")]), _ws())
    module = bundle.files["launchpad_builtin_tools.py"]
    assert "code_interpreter" in module and "AgentCoreCodeInterpreter" in module
    assert "tools.extend([code_interpreter])" in bundle.files["main.py"]
    assert "from launchpad_builtin_tools import code_interpreter" in bundle.files["main.py"]
    assert any(r.startswith("strands-agents-tools") for r in bundle.requirements)
    assert "launchpad_browser_tool.py" not in bundle.files  # browser only when selected
    _compile_all(bundle.files, tmp_path)


def test_builtin_browser_renders_its_own_module_only_when_selected(tmp_path):
    """N15-obs16 part 2: `browser` is a sibling module grafted on A2 like the code
    interpreter, without the strands-agents-tools pin (websockets rides on bedrock-agentcore);
    the module signs the automation stream from the boto3 endpoint (no Global host literal)."""
    plain = harness_local.render_harness_bundle(_spec(), _ws())
    assert "launchpad_browser_tool.py" not in plain.files
    assert harness_local.BROWSER_GRAFT_START not in plain.files["main.py"]
    bundle = harness_local.render_harness_bundle(
        _spec(tools=[ToolRef(type="builtin", name="browser")]), _ws())
    module = bundle.files["launchpad_browser_tool.py"]
    assert "def browser(url: str) -> str:" in module and "BrowserClient" in module
    assert 'BROWSER_IDENTIFIER = "aws.browser.v1"' in module
    assert ".amazonaws.com" not in module and "client.generate_ws_headers" not in module
    main = bundle.files["main.py"]
    assert "from launchpad_browser_tool import browser" in main
    assert "tools.extend([browser])" in main and harness_local.BROWSER_GRAFT_END in main
    assert "launchpad_builtin_tools.py" not in bundle.files
    assert not any(r.startswith("strands-agents-tools") for r in bundle.requirements)
    assert bundle.notes["builtin_tools"] == "wired (browser)"
    _compile_all(bundle.files, tmp_path)
    # both builtins: two modules, two grafts, the pin once
    both = harness_local.render_harness_bundle(
        _spec(tools=[ToolRef(type="builtin", name="code-interpreter"),
                     ToolRef(type="builtin", name="browser"),
                     ToolRef(type="builtin", name="browser")]), _ws())
    assert {"launchpad_builtin_tools.py", "launchpad_browser_tool.py"} <= set(both.files)
    assert both.files["main.py"].count("tools.extend([") == 2
    assert sum(r.startswith("strands-agents-tools") for r in both.requirements) == 1
    assert both.notes["builtin_tools"] == "wired (code_interpreter, browser)"


def test_every_runtime_builtin_has_a_render_module():
    """The request-boundary guard trusts BUILTIN_RUNTIME_TOOLS; each key must render."""
    assert set(harness_local.BUILTIN_MODULES) == set(harness_local.BUILTIN_RUNTIME_TOOLS)
    for module in harness_local.BUILTIN_MODULES.values():
        assert module.template.is_file(), module.template
    with pytest.raises(harness_local.HarnessRenderError, match="not-a-builtin"):
        harness_local.render_harness_bundle(
            _spec(tools=[ToolRef(type="builtin", name="not-a-builtin")]), _ws()
        )


def test_requirements_are_platform_plus_template_pins_plus_spec():
    bundle = harness_local.render_harness_bundle(_spec(requirements=["pyyaml==6.0.2"]), _ws())
    platform = zip_runtime.platform_requirements("harness", "bedrock")
    assert bundle.requirements[: len(platform)] == platform
    assert "mcp==1.19.0" in bundle.requirements and bundle.requirements[-1] == "pyyaml==6.0.2"
    # strands-agents-tools only rides along when a builtin tool is selected (R10)
    assert not any(r.startswith("strands-agents-tools") for r in bundle.requirements)


def test_template_requirements_strip_comments(tmp_path):
    (tmp_path / "requirements.txt.tmpl").write_text(
        "# frozen pins\nmcp==1.19.0  # export range\n\nbotocore[crt]==1.43.109\n")
    assert harness_local.template_requirements(tmp_path) == [
        "mcp==1.19.0", "botocore[crt]==1.43.109"
    ]
    assert harness_local.template_requirements(tmp_path / "absent") == []


def test_prompt_ending_in_a_double_quote_still_compiles(tmp_path):
    bundle = harness_local.render_harness_bundle(_spec(system_prompt='Say "hi"'), _ws())
    assert "_LAUNCHPAD_DEFAULT_SYSTEM_PROMPT = 'Say \"hi\"'" in bundle.files["main.py"]
    _compile_all(bundle.files, tmp_path)


def test_generate_and_package_stages_write_the_tree(tmp_path, monkeypatch):
    db = SessionLocal()
    agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="hr-helpdesk", method="harness",
                  status="deploying", spec=_spec(skills=["s3://b/skills/x/"]).model_dump())
    db.add(agent)
    db.commit()
    db.refresh(agent)
    db.close()
    uploads: list[tuple[str, str]] = []

    class S3:
        def upload_file(self, path, bucket, key):
            uploads.append((bucket, key))

    ws = _ws()
    # every AWS client comes out of the aws_clients funnel; fake it there (C8)
    monkeypatch.setattr(aws_clients, "client", lambda service, ctx, **_: S3())
    ctx = StageContext(agent_id=agent.id, deployment_id="d", job_id="j", workspace=ws)
    logged: list[str] = []
    ctx.log = logged.append
    written: dict[str, str] = {}

    def fake_build_zip(code, requirements, build_dir, *, on_pkg_ready=None, on_lock_ready=None):
        pkg = tmp_path / "pkg"
        pkg.mkdir(exist_ok=True)
        (pkg / "main.py").write_text(code)
        if on_pkg_ready:
            on_pkg_ready(pkg)
        for p in pkg.rglob("*"):
            if p.is_file():
                written[p.relative_to(pkg).as_posix()] = p.read_text()
        archive = tmp_path / "pkg.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("main.py", code)
        return archive

    monkeypatch.setattr(harness_local, "build_zip", fake_build_zip)
    monkeypatch.setattr(harness_local, "record_input_contract", lambda ctx, code: None)
    gen = harness_local._stage_generate(ctx, agent)
    assert "harness template" in gen.detail
    assert ctx.scratch["code"] == ctx.scratch["harness_bundle"].files["main.py"]
    assert ctx.scratch["requirements"] == ctx.scratch["harness_bundle"].requirements
    assert any(line.startswith("skills: wired") for line in logged)
    pkg = harness_local._stage_package(ctx, agent)
    assert uploads == [("launchpad-artifacts-test", "agents/hr-helpdesk/deployment_package.zip")]
    assert {"model/load.py", "memory/session.py", "mcp_client/client.py",
            "hooks/execution_limits.py", "skills/fetcher.py"} <= set(written)
    assert written["main.py"] == ctx.scratch["code"]
    assert not any(k.startswith("skills/x/") for k in written)  # no package-time snapshot (R5)
    assert "s3 ✓" in pkg.detail
    assert ctx.scratch["s3_key"] == "agents/hr-helpdesk/deployment_package.zip"
    ctx.scratch.clear()  # resumed job: an empty scratch re-renders instead of failing
    harness_local._stage_package(ctx, agent)
    assert len(uploads) == 2


def test_package_stage_requires_the_artifacts_bucket():
    agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="hr-helpdesk", method="harness",
                  status="deploying", spec=_spec().model_dump())
    ctx = StageContext(agent_id="a", deployment_id="d", job_id="j",
                       workspace=ws_ctx({}, region="cn-northwest-1"))
    with pytest.raises(RuntimeError, match="artifacts_bucket"):
        harness_local._stage_package(ctx, agent)


def test_check_template_accepts_the_fixture_and_names_missing_anchors(tmp_path):
    assert harness_local.check_template(FIXTURE_TEMPLATE) == []
    broken = _copy_fixture(tmp_path / "t")
    main = broken / "main.py.tmpl"
    main.write_text(main.read_text().replace("tools = []", "tools = list()"))
    assert any("A2" in problem for problem in harness_local.check_template(broken))


def test_check_template_reports_every_anchor_id_and_redaction(tmp_path):
    broken = _copy_fixture(tmp_path / "t")
    main = broken / "main.py.tmpl"
    text = main.read_text()
    text = text.replace("system_prompt=DEFAULT_SYSTEM_PROMPT", "system_prompt=PROMPT")  # A1
    text = text.replace("user_id = getattr(context, 'user_id', 'default-user')", "user_id = 'u'")
    text = text.replace("timeout_seconds = 300", "timeout_seconds = None")  # A8
    text = text.replace("from model.load import load_model", "from model.load import lm")  # A9
    text += "\nBUCKET = 's3.cn-northwest-1.amazonaws.com'\n"  # redaction
    main.write_text(text)
    client = broken / "mcp_client" / "client.py.tmpl"
    client.write_text(client.read_text() + "\nclient = MCPClient(lambda: None)\n")  # A4 inert
    (broken / "skills" / "fetcher.py.tmpl").unlink()
    problems = harness_local.check_template(broken)
    joined = "\n".join(problems)
    for expected in ("A1", "A4", "A7", "A8", "A9", "missing skills/fetcher.py.tmpl", "redaction"):
        assert expected in joined, (expected, problems)
    assert not any(p.startswith("render:") for p in problems)  # render only runs on a clean check
    assert harness_local.check_template(tmp_path / "absent") != []


def _import_rendered_bundle(files: dict[str, str], root: Path, monkeypatch, request):
    """Import the rendered tree as the runtime would (bundle root on sys.path), with the
    Strands Agent stubbed so construction is observable and no model is built. The
    bundle's packages are evicted from sys.modules at teardown (the session shim imports
    `launchpad_memory` lazily, so they must stay importable for the test's duration)."""
    import importlib.util
    import sys
    import types

    import strands

    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(strands, "Agent", lambda **kwargs: types.SimpleNamespace(**kwargs))
    before = set(sys.modules)
    bundle_packages = {"model", "memory", "skills", "hooks", "mcp_client", "launchpad_memory"}

    def _evict():
        for name in set(sys.modules) - before:
            if name.split(".")[0] in bundle_packages:
                sys.modules.pop(name, None)

    request.addfinalizer(_evict)
    spec = importlib.util.spec_from_file_location("rendered_harness_main", root / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, sys.modules["launchpad_memory"]


def test_rendered_bundle_wires_memory_hooks_and_recall(tmp_path, monkeypatch, request):
    """N8 item 2 on the fixture template: with the platform env the Agent gets
    ExecutionLimitsHook + LaunchpadMemoryHook and a snapshot session manager; without
    LAUNCHPAD_MEMORY_ID the agent still constructs with the limits hook alone."""
    spec = _spec(memory={"short_term": True, "long_term": True})
    bundle = harness_local.render_harness_bundle(spec, _ws())
    main_py = bundle.files["main.py"]
    for mark in (hc.MEMORY_GRAFT_START, hc.MEMORY_HOOKS_MARK, hc.MEMORY_RECALL_START):
        assert main_py.count(mark) == 1
    assert "launchpad_memory.py" in bundle.files
    _compile_all(bundle.files, tmp_path / "compiled")

    monkeypatch.delenv("LAUNCHPAD_MEMORY_ID", raising=False)
    monkeypatch.delenv("LAUNCHPAD_MEMORY_BUCKET", raising=False)
    module, lib = _import_rendered_bundle(bundle.files, tmp_path / "bundle", monkeypatch, request)
    monkeypatch.setattr(module, "load_model", lambda: "model")
    off = module.get_or_create_agent("sess-1", "hr-helpdesk__river", None)
    assert off.session_manager is None
    assert [type(h).__name__ for h in off.hooks] == ["ExecutionLimitsHook"]
    assert off.system_prompt == "You answer HR questions."

    monkeypatch.setenv("LAUNCHPAD_MEMORY_ID", "mem-1")
    monkeypatch.setenv("LAUNCHPAD_MEMORY_BUCKET", "bkt")

    class FakeSnapshot:  # the real one opens the S3 session at construction
        def __init__(self, actor_id, session_id):
            self.actor_id, self.session_id = actor_id, session_id

    monkeypatch.setattr(lib, "SnapshotSessionManager", FakeSnapshot)
    on = module.get_or_create_agent("sess-2", "hr-helpdesk__river", None)
    assert (on.session_manager.actor_id, on.session_manager.session_id) == (
        "hr-helpdesk__river", "sess-2"
    )
    assert [type(h).__name__ for h in on.hooks] == ["ExecutionLimitsHook", "LaunchpadMemoryHook"]
    assert (on.hooks[1].actor_id, on.hooks[1].session_id) == ("hr-helpdesk__river", "sess-2")
    assert module.make_memory_hooks is lib.make_memory_hooks
    assert module.recall_context is lib.recall_context
    assert module._launchpad_recall_query("hi") == "hi"
    # unsafe ids are refused inside the library, so the hook list stays limits-only
    unsafe = module.get_or_create_agent("../x", "hr-helpdesk__river", None)
    assert [type(h).__name__ for h in unsafe.hooks] == ["ExecutionLimitsHook"]


def test_real_template_renders_memory_grafts_and_compiles(tmp_path, monkeypatch):
    monkeypatch.setattr(harness_local, "TEMPLATE_DIR", harness_local.TEMPLATES / "harness_runtime")
    bundle = harness_local.render_harness_bundle(_spec(memory={"short_term": True}), _ws())
    main_py = bundle.files["main.py"]
    assert "*make_memory_hooks(user_id, session_id)" in main_py
    assert "recall_context(user_id, _launchpad_query)" in main_py
    _compile_all(bundle.files, tmp_path)
    assert harness_local.check_template(harness_local.TEMPLATES / "harness_runtime") == []
