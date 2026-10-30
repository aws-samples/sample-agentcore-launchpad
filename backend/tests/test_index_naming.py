"""D17 — one shared OpenSearch domain for every EnvName environment; isolation moves to
index prefixes + FGAC. The guarantees pinned here:

1. The default environment keeps today's index names byte for byte; a named one prefixes
   every index with `launchpad-<env>-` — through ONE rule, `app.core.naming.index_name`,
   whose body is mirrored into the OpenSearch core block (and from there into the runtime
   template and the extractor Lambda, which read the env from `LAUNCHPAD_ENV_NAME`).
2. The per-environment FGAC role `launchpad_memory_rw[_<env>]` can match every index its
   own environment creates and none that any other environment creates.
3. `kb` is a reserved env name in all three layers (its prefix would be the default
   environment's `launchpad-kb-*` pattern).
"""

import fnmatch
import importlib.util
import inspect
import itertools
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core import naming
from app.core.config import Settings
from app.deployer.environment import runtime_environment
from app.schemas.agent import AgentSpec
from app.services import agent_iam
from app.services import memory_bootstrap as mb
from app.services import opensearch_http as core
from app.templates.strands_agent import render_main_py

REPO = Path(__file__).parents[2]
INFRA_NAMING = REPO / "infra" / "stacks" / "naming.py"
EC2_TEMPLATE = REPO / "infra" / "ec2" / "launchpad-cn-ec2.yaml"
LAMBDA_DIR = REPO / "backend" / "lambdas" / "memory_extractor"
KB_TEMPLATE = REPO / "backend" / "app" / "templates" / "direct_kb_tools.py.tmpl"

ENVS = ["", "a", "t1", "t10", "1", "abc", "me", "mem", "job", "doc", "kbx", "999"]
KB_IDS = ["kb1", "KB111", "memory", "jobs", "docs", "t1-memory", "x", "kb-docs"]


@pytest.fixture
def env_t1(monkeypatch):
    monkeypatch.setattr(naming, "current_env_name", lambda: "t1")


@pytest.fixture
def env_default(monkeypatch):
    monkeypatch.setattr(naming, "current_env_name", lambda: "")


def _indices_of(env: str) -> list[str]:
    """Every index name an environment can create."""
    return [
        naming.memory_index(env),
        naming.jobs_index(env),
        naming.kb_docs_index(env),
        *(naming.kb_index(kb, env) for kb in KB_IDS),
    ]


def _matches(patterns: list[str], index: str) -> bool:
    return any(fnmatch.fnmatchcase(index, pattern) for pattern in patterns)


# ── 1. the rule ──────────────────────────────────────────────────────────────


def test_default_environment_keeps_todays_index_names(env_default):
    assert naming.memory_index() == "launchpad-memory"
    assert naming.jobs_index() == "launchpad-jobs"
    assert naming.kb_docs_index() == "launchpad-kb-docs"
    assert naming.kb_index("KB111") == "launchpad-kb-kb111"
    assert naming.security_role_name() == "launchpad_memory_rw"
    assert naming.index_patterns() == ["launchpad-memory*", "launchpad-jobs*", "launchpad-kb-*"]


def test_named_environment_prefixes_every_index_once(env_t1):
    assert naming.memory_index() == "launchpad-t1-memory"
    assert naming.jobs_index() == "launchpad-t1-jobs"
    assert naming.kb_docs_index() == "launchpad-t1-kb-docs"
    assert naming.kb_index("KB111") == "launchpad-t1-kb-kb111"
    assert naming.security_role_name() == "launchpad_memory_rw_t1"
    assert naming.index_patterns() == ["launchpad-t1-*"]
    for index in _indices_of("t1"):
        assert index.startswith("launchpad-t1-"), index
        assert not index.startswith("launchpad-t1-t1-"), index


def test_explicit_env_argument_overrides_the_setting(env_t1):
    assert naming.memory_index("") == "launchpad-memory"
    assert naming.index_name("memory", "abc") == "launchpad-abc-memory"
    with pytest.raises(ValueError, match="env_name"):
        naming.index_name("memory", "Bad")


def test_the_rule_is_mirrored_byte_for_byte_into_the_opensearch_core():
    """`opensearch_http.index_name` (the copy the runtime template and the extractor
    Lambda carry, since neither can import `app.*`) must compute exactly what
    `naming.index_name` does — same expression, same results."""
    rule = 'return f"launchpad-{base}" if not env_name else f"launchpad-{env_name}-{base}"'
    assert rule in inspect.getsource(naming.index_name)
    assert rule in inspect.getsource(core.index_name)
    for env, base in itertools.product(ENVS, ["memory", "jobs", "kb-docs", "kb-kb1"]):
        assert naming.index_name(base, env) == core.index_name(base, env)
    for env, kb in itertools.product(ENVS, KB_IDS):
        assert naming.kb_index(kb, env) == core.kb_index_name(kb, env)
    for env in ENVS:
        assert naming.memory_index(env) == core.memory_index(env)
        assert naming.jobs_index(env) == core.jobs_index(env)
        assert naming.kb_docs_index(env) == core.kb_docs_index(env)


def test_core_helpers_require_an_explicit_environment():
    """No default for `env_name` in the mirrored copy: a call site that forgets it must
    fail loudly rather than silently address the default environment's indices."""
    for fn in (core.index_name, core.memory_index, core.jobs_index, core.kb_docs_index,
               core.kb_index_name):
        param = inspect.signature(fn).parameters["env_name"]
        assert param.default is inspect.Parameter.empty, fn.__name__
    assert not hasattr(core, "MEMORY_INDEX")
    assert not hasattr(core, "JOBS_INDEX")
    assert not hasattr(core, "KB_DOCS_INDEX")
    assert core.INDEX_ENV_VAR == naming.ENV_NAME_ENV_VAR == "LAUNCHPAD_ENV_NAME"


# ── 2. FGAC isolation ────────────────────────────────────────────────────────


@pytest.mark.parametrize("env", ENVS)
def test_an_environment_role_covers_every_index_it_creates(env):
    patterns = naming.index_patterns(env)
    for index in _indices_of(env):
        assert _matches(patterns, index), (env, index, patterns)


@pytest.mark.parametrize("env_a,env_b", [(a, b) for a in ENVS for b in ENVS if a != b])
def test_an_environment_role_matches_nothing_another_environment_creates(env_a, env_b):
    patterns = naming.index_patterns(env_a)
    for index in _indices_of(env_b):
        assert not _matches(patterns, index), (env_a, patterns, env_b, index)


def test_default_role_never_uses_the_bare_launchpad_wildcard():
    for env in ENVS:
        assert "launchpad-*" not in naming.index_patterns(env)


# ── 3. the reserved environment name ─────────────────────────────────────────


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_env_name_kb_is_reserved_in_all_three_layers(monkeypatch):
    # it would otherwise be a valid pattern match — the collision is the only reason
    assert naming._ENV_NAME_RE.fullmatch("kb")
    with pytest.raises(ValueError, match="reserved"):
        naming.validate_env_name("kb")
    with pytest.raises(ValueError, match="reserved"):
        naming.index_name("memory", "kb")
    monkeypatch.setenv("LAUNCHPAD_ENV_NAME", "kb")
    with pytest.raises(ValidationError, match="reserved"):
        Settings(_env_file=None)
    infra_naming = _load(INFRA_NAMING, "launchpad_infra_naming_d17")
    assert infra_naming.RESERVED_ENV_NAMES == naming.RESERVED_ENV_NAMES == frozenset({"kb"})
    with pytest.raises(ValueError, match="reserved"):
        infra_naming.validate_env_name("kb")
    template = EC2_TEMPLATE.read_text(encoding="utf-8")
    assert "EnvNameIsNotReserved:" in template
    assert '!Not [!Equals [!Ref EnvName, "kb"]]' in template


def test_why_kb_is_reserved():
    """The one collision the rule has: an environment "kb" would create
    `launchpad-kb-memory`, which the default environment's `launchpad-kb-*` (its per-KB
    index pattern) matches — and which IS the default environment's index for a KB with
    id "memory"."""
    default_patterns = naming.index_patterns("")
    assert _matches(default_patterns, core.index_name("memory", "kb"))
    assert core.index_name("memory", "kb") == naming.kb_index("memory", "")


# ── 4. the consumers ─────────────────────────────────────────────────────────


def test_memory_bootstrap_provisions_only_this_environments_slice(env_t1):
    from tests.test_memory_bootstrap import OUTPUTS, SecOS

    os_ = SecOS()
    kw = dict(exec_role_arn="e", extractor_role_arn="x", agent_role_pattern="p", log=lambda m: None)
    out = mb.ensure_memory_substrate(os_, **kw)
    assert out["created"] == [
        "index launchpad-t1-memory",
        "index launchpad-t1-jobs",
        "index launchpad-t1-kb-docs",
        "security role launchpad_memory_rw_t1",
        "role mapping launchpad_memory_rw_t1",
    ]
    assert set(os_.indices) == {"launchpad-t1-memory", "launchpad-t1-jobs", "launchpad-t1-kb-docs"}
    role = os_.security["/_plugins/_security/api/roles/launchpad_memory_rw_t1"]
    assert role["index_permissions"] == [
        {"index_patterns": ["launchpad-t1-*"], "allowed_actions": ["indices_all"]}
    ]
    assert os_.security["/_plugins/_security/api/rolesmapping/launchpad_memory_rw_t1"] == {
        "users": ["e", "x", "p"]
    }
    assert all(path.endswith("_t1") for path in os_.security), list(os_.security)
    # the resource map hands the runtime library the same name
    res = mb.memory_resources(OUTPUTS, bucket="b", store_id="s")
    assert res["memory_index"] == "launchpad-t1-memory"
    # a second pass is a no-op, as in the default environment
    again = mb.ensure_memory_substrate(os_, **kw)
    # r14-a: the reused memory index also reports its (no-op) v2 mapping check
    assert again["created"] == [] and len(again["reused"]) == 6
    assert "mapping launchpad-t1-memory v2 fields" in again["reused"]
    mb.map_agent_role(os_, "arn:aws-cn:iam::111:role/launchpad-agent-a-1234-t1")
    mapping = os_.security["/_plugins/_security/api/rolesmapping/launchpad_memory_rw_t1"]
    assert mapping["users"][-1] == "arn:aws-cn:iam::111:role/launchpad-agent-a-1234-t1"


def test_stack_names_env_stack_is_suffixed_domain_stack_is_not(env_t1):
    assert mb.memory_stack_name() == "launchpad-memory-t1"
    assert mb.memory_domain_stack_name() == "launchpad-memory-domain"


def test_agent_role_statements_name_the_environments_indices(env_t1):
    from tests.test_agent_iam_policy import MEM_CTX

    spec = AgentSpec(
        name="probe", method="zip_runtime", system_prompt="p",
        memory={"short_term": True, "long_term": True},
        knowledge_bases=[{"kb_id": "KB1"}],
    )
    by = {s["Sid"]: s for s in agent_iam.policy_document(spec, MEM_CTX)["Statement"]}
    assert by["MemorySearch"]["Resource"] == [f"{MEM_CTX.memory_domain_arn}/launchpad-t1-memory/*"]
    assert by["KnowledgeSearch"]["Resource"] == [
        f"{MEM_CTX.memory_domain_arn}/launchpad-t1-kb-kb1/*"
    ]


def test_runtime_environment_carries_the_env_name_only_for_a_named_environment(
    env_t1, monkeypatch
):
    from tests.test_runtime_environment import PLAIN, RESOURCES, SUBSTRATE

    assert runtime_environment(PLAIN, SUBSTRATE)["LAUNCHPAD_ENV_NAME"] == "t1"
    # no substrate → no OpenSearch → nothing to prefix
    assert "LAUNCHPAD_ENV_NAME" not in runtime_environment(PLAIN, RESOURCES)
    monkeypatch.setattr(naming, "current_env_name", lambda: "")
    assert "LAUNCHPAD_ENV_NAME" not in runtime_environment(PLAIN, SUBSTRATE)


def _load_handler(monkeypatch, env_name: str | None, stem: str):
    if env_name is None:
        monkeypatch.delenv("LAUNCHPAD_ENV_NAME", raising=False)
    else:
        monkeypatch.setenv("LAUNCHPAD_ENV_NAME", env_name)
    monkeypatch.syspath_prepend(str(LAMBDA_DIR))
    sys.modules.pop("opensearch_http", None)
    try:
        return _load(LAMBDA_DIR / "handler.py", stem)
    finally:
        sys.modules.pop("opensearch_http", None)


def test_extractor_lambda_resolves_its_indices_from_the_env_var(monkeypatch):
    default = _load_handler(monkeypatch, None, "d17_handler_default")
    assert (default.MEMORY_INDEX, default.JOBS_INDEX, default.KB_DOCS_INDEX) == (
        "launchpad-memory", "launchpad-jobs", "launchpad-kb-docs",
    )
    t1 = _load_handler(monkeypatch, "t1", "d17_handler_t1")
    assert (t1.MEMORY_INDEX, t1.JOBS_INDEX, t1.KB_DOCS_INDEX) == (
        "launchpad-t1-memory", "launchpad-t1-jobs", "launchpad-t1-kb-docs",
    )
    assert t1.ENV_NAME == "t1"


def test_runtime_library_resolves_its_indices_from_the_env_var(tmp_path, monkeypatch):
    from tests.test_memory_runtime import MEM_ENV, SPEC
    from tests.test_memory_runtime import _load as load_runtime

    for key, value in MEM_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LAUNCHPAD_ENV_NAME", raising=False)
    default = load_runtime(SPEC, tmp_path, monkeypatch, "d17_rt_default")
    assert default.LAUNCHPAD_ENV_NAME == "" and default.MEMORY_INDEX == "launchpad-memory"
    assert default.launchpad_kb_index("KB111") == "launchpad-kb-kb111"

    monkeypatch.setenv("LAUNCHPAD_ENV_NAME", "t1")
    t1 = load_runtime(SPEC, tmp_path, monkeypatch, "d17_rt_t1")
    assert t1.MEMORY_INDEX == "launchpad-t1-memory"
    assert t1.launchpad_kb_index("KB111") == "launchpad-t1-kb-kb111"
    # recall really queries the prefixed index
    os_ = t1._fakes["os"]
    os_.ensure_index("launchpad-t1-memory", core.memory_index_body())
    os_.put_doc("launchpad-t1-memory", "r1", {
        "store_id": MEM_ENV["LAUNCHPAD_MEMORY_ID"], "namespace": "/facts/agent1__river",
        "text": "likes tea", "embedding": t1._fakes["emb"].embed("likes tea"),
    })
    hits = t1.OpenSearchMemoryStore("agent1__river").search("tea", ["/facts/agent1__river"])
    assert [h["text"] for h in hits] == ["likes tea"]
    assert not os_.indices.get("launchpad-memory")  # the default index was never touched


def test_direct_kb_tools_resolve_the_index_through_the_library(tmp_path, monkeypatch):
    from tests.test_strands_template import KB_SPEC, _import_rendered

    monkeypatch.setenv("LAUNCHPAD_ENV_NAME", "t1")
    module = _import_rendered(KB_SPEC, tmp_path, monkeypatch, "d17_kb_t1")
    assert module._kb_index("KB111") == "launchpad-t1-kb-kb111"
    monkeypatch.delenv("LAUNCHPAD_ENV_NAME", raising=False)
    module = _import_rendered(KB_SPEC, tmp_path, monkeypatch, "d17_kb_default")
    assert module._kb_index("KB111") == "launchpad-kb-kb111"
    # the KB template never computes a name itself — only the library does
    kb_template = KB_TEMPLATE.read_text(encoding="utf-8")
    assert '_kb_lib()["launchpad_kb_index"](kb_id)' in kb_template
    assert 'f"launchpad-' not in kb_template


def test_rendered_agent_reads_the_env_var_outside_the_mirrored_block():
    """The core block stays byte-identical across its three copies, so the env-var read
    and the derived names live right after it (`test_memory_core_mirror.py` pins the
    block itself)."""
    code = render_main_py(AgentSpec(name="probe", method="zip_runtime", system_prompt="p"))
    start = code.index("# <launchpad-opensearch-core>")
    end = code.index("# </launchpad-opensearch-core>")
    tail = code[end:]
    assert 'LAUNCHPAD_ENV_NAME = os.environ.get(INDEX_ENV_VAR, "")' in tail
    assert "MEMORY_INDEX = memory_index(LAUNCHPAD_ENV_NAME)" in tail
    assert "def launchpad_kb_index(kb_id: str) -> str:" in tail
    # no literal index name survives inside the block — only the rule
    assert '"launchpad-memory"' not in code[start:end]
    assert '"launchpad-kb-' not in code[start:end]
