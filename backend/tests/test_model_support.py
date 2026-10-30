"""R4: the Harness model/load.py is the Strands template's model block, sliced — one source."""
import ast
import py_compile

from app.schemas.agent import AgentSpec
from app.templates import model_support
from app.templates.strands_agent import render_main_py


def _spec(**kw):
    return AgentSpec(name="probe", method="harness", system_prompt="p", **kw)


def test_markers_bracket_exactly_the_model_block():
    src = model_support.STRANDS_TEMPLATE.read_text()
    for start, end in (model_support.MODEL_CONSTANTS_MARKERS, model_support.MODEL_HELPERS_MARKERS):
        assert src.count(start) == 1 and src.count(end) == 1 and src.index(start) < src.index(end)
    block = model_support.render_model_source(_spec(max_tokens=8192))
    for needle in (
        'MODEL_ID = "global.zai.glm-5.3"',
        'MODEL_REGION = os.environ.get("LAUNCHPAD_MODEL_REGION", "us-west-2")',
        'MODEL_CREDENTIAL = os.environ.get("LAUNCHPAD_MODEL_CREDENTIAL", '
        '"launchpad-global-bedrock")',
        "MODEL_MAX_TOKENS = 8192", "def _resolve_bedrock_api_key()",
        "def _launchpad_bedrock_model(",
        # N12-bug11: the catalog's inference-parameter table is inlined, not imported
        "MODEL_INFERENCE_PARAMS: dict[str, dict[str, bool]] = {",
        "    'global.zai.glm-5.3': {'temperature': True},",
        "    'global.moonshotai.kimi-k3': {'temperature': False},",
        "def _supported_model_config(", "BedrockModel(**_supported_model_config(config))",
        "api_key=_resolve_bedrock_api_key()", "region_name=MODEL_REGION", "def build_model()",
    ):
        assert needle in block
    assert "__LAUNCHPAD_" not in block and "DEFAULT_SYSTEM_PROMPT" not in block


def test_markers_are_comment_lines_only():
    """The markers must not change the rendered Strands agent (N2's tests stay untouched)."""
    src = model_support.STRANDS_TEMPLATE.read_text()
    for marker in (*model_support.MODEL_CONSTANTS_MARKERS, *model_support.MODEL_HELPERS_MARKERS):
        line = next(ln for ln in src.splitlines() if marker in ln)
        assert line.startswith(marker)
        assert line[len(marker):].strip() in ("", "# --- CN harness (N7) ---")


def test_model_block_is_byte_identical_to_the_rendered_strands_template():
    harness = _spec(max_tokens=16384)
    zip_spec = AgentSpec(**{**harness.model_dump(), "method": "zip_runtime", "max_tokens": None})
    rendered = render_main_py(zip_spec)  # same max tokens: 16384 is the GLM default (N10-bug4)
    for chunk in model_support.render_model_source(harness).split("\n\n\n"):
        assert chunk.strip() in rendered


def test_model_load_is_self_contained():
    """Every name the sliced block reads is defined by the slice or the load.py header."""
    source = model_support.render_model_load(_spec())
    tree = ast.parse(source)
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        defined |= {t.id for t in targets if isinstance(t, ast.Name)}
    imported = {
        alias.asname or alias.name.split(".")[0]
        for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    for needed in ("MODEL_ID", "MODEL_REGION", "MODEL_CREDENTIAL", "MODEL_MAX_TOKENS",
                   "BEDROCK_BEARER_ENV", "_BEDROCK_API_KEY", "_identity_client",
                   "_workload_access_token", "_resolve_bedrock_api_key",
                   "_launchpad_bedrock_model", "build_model", "load_model",
                   "MODEL_INFERENCE_PARAMS", "_supported_model_config"):
        assert needed in defined, needed
    for needed in ("os", "Any", "BedrockAgentCoreContext", "BedrockModel", "IdentityClient"):
        assert needed in imported, needed
    # the platform's own constants and the prompt never leak into the model module
    assert "AGENT_NAME" not in source and "SKILLS_ENABLED" not in source


def test_model_load_has_no_free_names():
    """R4 drift guard: every name the sliced block reads is bound in load.py itself or is a
    builtin — a later N2 edit that reads e.g. a module-level `logger` from inside the block
    fails here, not as a NameError on the first model call."""
    import builtins

    tree = ast.parse(model_support.render_model_load(_spec()))
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
    assert loads - bound == set(), f"free names in model/load.py: {sorted(loads - bound)}"


def test_model_load_compiles_and_exposes_load_model(tmp_path):
    source = model_support.render_model_load(_spec())
    assert "def load_model()" in source and "return build_model()" in source
    path = tmp_path / "load.py"
    path.write_text(source)
    py_compile.compile(str(path), doraise=True)


def test_harness_model_load_defaults_to_the_model_aware_ceiling_and_explicit_wins():
    # N10-bug4 (live): a Harness on GLM 5.3 with `max_tokens` unset rendered 4096 and a
    # skill-mounted turn stopped at the limit. The Harness bundle's model/load.py is
    # rendered here (deployer/harness_local.py only calls render_model_load), so this is
    # the one place the default is applied for every generated runtime.
    assert "MODEL_MAX_TOKENS = 16384" in model_support.render_model_load(_spec())
    assert "MODEL_MAX_TOKENS = 16384" in model_support.render_model_load(
        _spec(model_id="global.moonshotai.kimi-k3")
    )
    # an explicit spec value is never overridden, below or above the default
    assert "MODEL_MAX_TOKENS = 8192" in model_support.render_model_load(_spec(max_tokens=8192))
    assert "MODEL_MAX_TOKENS = 65536" in model_support.render_model_load(_spec(max_tokens=65536))


# ── D21 (M1-d): catalog version + descriptor in the block; seed data literals pinned ──
def test_block_renders_the_catalog_version_and_the_models_descriptor():
    from app.schemas.agent import catalog_version, model_descriptor

    block = model_support.render_model_source(_spec())
    assert f"MODEL_CATALOG_VERSION = {catalog_version()!r}" in block
    descriptor = model_descriptor("global.zai.glm-5.3")
    assert f"MODEL_DESCRIPTOR: dict[str, Any] | None = {descriptor!r}" in block
    assert 'MODEL_DESCRIPTOR_ENV = "LAUNCHPAD_MODEL_DESCRIPTOR"' in block
    # the registry helpers are part of the sliced block (so Harness / Studio / worker get them)
    for needle in ("MODEL_DESCRIPTORS: dict[str, dict[str, Any]] = {}",
                   "def _launchpad_register_descriptors(", "def _launchpad_env_descriptors(",
                   "_launchpad_register_descriptors(MODEL_DESCRIPTOR)",
                   "descriptor = MODEL_DESCRIPTORS.get(model_id)"):
        assert needle in block, needle
    # an id outside the catalog renders None, never a KeyError
    assert "MODEL_DESCRIPTOR: dict[str, Any] | None = None" in model_support.render_model_block(
        model_id="custom.model", model_region="us-west-2",
        model_credential="launchpad-global-bedrock", max_tokens=1024)


def _legacy_seed_tables():
    """The pre-D21 module tables: the `MODEL_CATALOG` dict captured by executing the pre-D21
    `schemas/agent.py` (tests/fixtures/model_catalog_pre_d21.json). The seed builder must
    still reproduce it (rev-r8 N1 on m1-d: pin against the frozen capture, not the builder)."""
    import json
    from pathlib import Path

    from app.schemas import model_catalog as mc

    # R13 (N13-obs7): the seed is the capture's Bedrock half — SiliconFlow left the seed
    fixture = {k: v for k, v in json.loads(
        (Path(__file__).parent / "fixtures" / "model_catalog_pre_d21.json")
        .read_text(encoding="utf-8"))["MODEL_CATALOG"].items()
        if not k.startswith("siliconflow.")}
    rebuilt = mc.build_model_catalog()
    assert {k: dict(v) for k, v in rebuilt.items()} == fixture
    assert list(rebuilt) == list(fixture)
    return fixture


def test_seed_data_literals_are_byte_identical_to_the_pre_d21_tables():
    """G6: with the seeded (or built-in) catalog, every DATA literal the block inlines —
    the provider table, the inference-parameter table and the worker's judge-budget table —
    renders byte for byte what the pre-D21 module dicts rendered. Only code (the descriptor
    registry + MODEL_CATALOG_VERSION) was added to the template text."""
    from app.schemas.agent import INFERENCE_PARAMS
    from app.templates.eval_worker import rendered_judge_budget_defaults
    from app.templates.strands_agent import (
        rendered_model_inference_params,
        rendered_model_providers,
    )

    legacy = _legacy_seed_tables()
    providers = ["{"]
    params = ["{"]
    budgets = ["{"]
    for model_id, entry in legacy.items():
        row = {"kind": entry["kind"], "base_url": entry["base_url"],
               "api_model_name": entry["api_model_name"],
               "credential_provider": entry["credential_provider"],
               "streaming": entry["streaming"]}
        providers.append(f"    {model_id!r}: {row!r},")
        accepts = {p: entry.get(p, True) for p in INFERENCE_PARAMS}
        params.append(f"    {model_id!r}: {accepts!r},")
        if entry.get("default_judge_max_tokens"):
            budgets.append(f"    {model_id!r}: {entry['default_judge_max_tokens']!r},")
    for lines in (providers, params, budgets):
        lines.append("}")
    assert rendered_model_providers() == "\n".join(providers)
    assert rendered_model_inference_params() == "\n".join(params)
    assert rendered_judge_budget_defaults() == "\n".join(budgets)
    # the seed carries exactly the two Bedrock rows, in order (R13: no SiliconFlow rows)
    assert list(legacy) == list(_legacy_seed_tables()) == [
        "global.zai.glm-5.3", "global.moonshotai.kimi-k3"]
