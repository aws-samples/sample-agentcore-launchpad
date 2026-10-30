"""rev-r16 FIX-FIRST on N16-obs72: the MERGED runtime environment (spec + platform keys) is
asserted ≤ the Runtime's 50 at second 1 of the `generate` stage with a named reason
(`agent.env_budget_exceeded {user, platform, limit}`) — never ~130 s later as
CreateAgentRuntime's ValidationException.
"""

from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.deployer import byoc as byoc_dep
from app.deployer import environment as env_mod
from app.deployer import zip_runtime as zr
from app.deployer.pipeline import StageContext
from app.schemas.agent import AGENT_ENV_MAX_VARS, AgentSpec, KnowledgeBaseRef, MemoryConfig, ToolRef
from app.services import memory_selfhosted
from tests.conftest import ws_ctx
from tests.test_byoc import (
    RESOURCES as BYOC_RESOURCES,
)
from tests.test_byoc import (  # noqa: I001
    SDK_MAIN,
    StubS3,
    _byoc_spec,
    _client_router,
    _get_agent,
    _mk_agent,
    _seed_upload,
    _stage_ctx,
)
from tests.test_runtime_environment import PLAIN, RESOURCES
from tests.test_zip_runtime_deployer import _fake_ws, _persist

AT_CAP = {f"USER_{i:02d}": "v" for i in range(AGENT_ENV_MAX_VARS)}


def _everything_on(monkeypatch) -> tuple[AgentSpec, dict]:
    """A spec at the member cap with every platform flag on (the N16-obs72 maximal set)."""
    from app.core import naming
    from app.core.config import get_settings

    monkeypatch.setattr(memory_selfhosted, "recall_env_for", lambda mid: {
        memory_selfhosted.ENV_MEMORY_HALF_LIFE_DAYS: "7",
        memory_selfhosted.ENV_MEMORY_TTL_DAYS: "30",
    })
    monkeypatch.setattr(naming, "current_env_name", lambda: "t1")
    settings = get_settings()
    monkeypatch.setattr(settings, "max_tokens_continuations", 3)
    monkeypatch.setattr(settings, "continuation_max_tokens", 64)
    spec = PLAIN.model_copy(update={
        "memory": MemoryConfig(short_term=True, long_term=True),
        "knowledge_bases": [KnowledgeBaseRef(kb_id="kb1", name="kb")],
        "tools": [ToolRef(type="gateway", name="launchpad-gw"),
                  ToolRef(type="rest", name="crm", config={"url": "https://example.com"},
                          auth={"connection": "crm-conn", "kind": "oauth2", "mode": "as_user"})],
        "env": dict(AT_CAP),
    })
    resources = {
        **RESOURCES, "artifacts_bucket": "artifacts", "memory_bucket": "mem",
        "memory_queue_url": "https://sqs/q", "memory_os_endpoint": "https://os.example",
        "memory_prefix": "memory",
    }
    # Global parity (2026-10-27): no deploy-time narrowing — the environment carries no
    # LAUNCHPAD_POLICY_* keys and runtime_environment takes no policy argument.
    return spec, resources


def test_a_spec_at_the_cap_with_every_platform_flag_on_fits_the_runtime_limit(monkeypatch):
    spec, resources = _everything_on(monkeypatch)
    environment = env_mod.runtime_environment(
        spec, resources, workload_name="wl-1", agent_id="ag-1",
    )
    environment.setdefault("MODEL_ID", spec.model_id)  # byoc's two on top
    environment.setdefault("ALLOWED_MODEL_IDS", "x")
    assert len(environment) <= env_mod.RUNTIME_ENV_LIMIT == 50
    assert set(AT_CAP) <= set(environment)
    env_mod.check_env_budget(environment, spec)  # no refusal at exactly the budget


def test_check_env_budget_names_the_reason_and_the_split():
    spec = PLAIN.model_copy(update={"env": {f"U{i}": "v" for i in range(30)}})
    merged = {**spec.env, **{f"LAUNCHPAD_P{i}": "v" for i in range(21)}}  # 51
    with pytest.raises(AppError) as exc:
        env_mod.check_env_budget(merged, spec)
    assert exc.value.code == "agent.env_budget_exceeded" and exc.value.status_code == 422
    assert exc.value.detail == {"user": 30, "platform": 21, "limit": 50}
    assert str(exc.value).startswith("agent.env_budget_exceeded:")  # the job log's detail
    assert "51 variables" in str(exc.value) and "remove 1" in str(exc.value)
    env_mod.check_env_budget(dict(list(merged.items())[:50]), spec)  # 50 passes


def test_runtime_environment_refuses_a_forced_over_budget_merged_map(monkeypatch):
    """Forced: the Runtime limit lowered below what the platform injects on its own."""
    monkeypatch.setattr(env_mod, "RUNTIME_ENV_LIMIT", 2)
    with pytest.raises(AppError) as exc:
        env_mod.runtime_environment(PLAIN, RESOURCES)
    assert exc.value.code == "agent.env_budget_exceeded"
    assert exc.value.detail == {"user": 0, "platform": 3, "limit": 2}


def test_zip_generate_stage_fails_at_second_one_before_any_aws_call(monkeypatch):
    from app.services import aws_clients

    monkeypatch.setattr(aws_clients, "client",
                        lambda *a, **k: pytest.fail("an AWS client was built"))
    monkeypatch.setattr(aws_clients, "get_session",
                        lambda *a, **k: pytest.fail("an AWS session was built"))
    agent = _persist("zip-env-budget", status="deploying")
    ctx = StageContext(agent_id=agent.id, deployment_id="d", job_id="j", workspace=_fake_ws())
    assert zr._stage_generate(ctx, agent).detail  # within budget: the stage runs as before
    monkeypatch.setattr(env_mod, "RUNTIME_ENV_LIMIT", 1)
    with pytest.raises(AppError) as exc:
        zr._stage_generate(ctx, agent)
    assert exc.value.code == "agent.env_budget_exceeded"
    assert exc.value.detail["limit"] == 1 and exc.value.detail["user"] == 0


def test_byoc_generate_stage_fails_at_second_one_before_any_aws_call(monkeypatch):
    from app.services import workspace as workspace_mod

    s3 = StubS3()
    _client_router(monkeypatch, {"s3": s3})
    _seed_upload(s3, "u1", {"main.py": SDK_MAIN})
    spec = AgentSpec(**_byoc_spec())
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(BYOC_RESOURCES))
    assert "code_zip" in byoc_dep._stage_generate(ctx, _get_agent(agent_id)).detail
    # over budget: refused before the upload manifest (S3) is even read
    monkeypatch.setattr(env_mod, "RUNTIME_ENV_LIMIT", 1)
    monkeypatch.setattr(workspace_mod.WorkspaceContext, "client",
                        lambda self, name, **_kw: pytest.fail(f"AWS client {name} was built"))
    with pytest.raises(AppError) as exc:
        byoc_dep._stage_generate(ctx, _get_agent(agent_id))
    assert exc.value.code == "agent.env_budget_exceeded"
    # byoc's MODEL_ID / ALLOWED_MODEL_IDS count as platform keys
    assert exc.value.detail["platform"] >= 4 and exc.value.detail["user"] == 0
