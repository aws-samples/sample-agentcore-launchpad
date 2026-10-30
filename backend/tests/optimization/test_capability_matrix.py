"""N12-obs34/35 — canary eligibility by what the deployed artifact supports (the method
registry's declared capabilities), the runner paths a CN self-built Harness agent takes
(canary create + candidate mint), and the console's reason-code i18n coverage. The
configuration-experiment twin retired with its routes (N13-obs2). Hermetic: stubbed AWS,
fake pip."""

import ast
import json
import re
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.optimization.canary_infra as infra
import app.optimization.canary_service as canary_svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer import harness_local
from app.deployer.environment import ENV_AGENT_ID, ENV_WORKLOAD
from app.deployer.pipeline import MethodCapabilities, method_capabilities
from app.models.ledger import Agent
from app.optimization import capability
from app.schemas.agent import AgentSpec
from app.services.harness_convert import GRAFT_START
from tests.conftest import ws_ctx

REPO = Path(__file__).resolve().parents[3]
LOCALES = REPO / "frontend" / "src" / "locales"
CAPABILITY_SRC = REPO / "backend" / "app" / "optimization" / "capability.py"
FIXTURE_TEMPLATE = Path(__file__).resolve().parents[1] / "fixtures" / "harness_runtime_min"
RUNTIME_ARN = (
    "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/hr_helpdesk-abcdefghij"
)
ELIGIBLE_CANARY = {"eligible": True, "reason": None, "reason_code": None}


def _row(method, *, spec=None, status="active", arn=RUNTIME_ARN, system_key=None):
    return SimpleNamespace(
        method=method, spec=spec if spec is not None else {"protocol": "http"},
        status=status, arn=arn, system_key=system_key,
    )


# ─── the declared per-method capabilities ─────────────────────────────────────
def test_method_registry_declares_artifact_capabilities():
    """The method modules declare, next to their stages, what their artifact supports —
    the capability projections read this instead of a hard-coded method set."""
    def artifact(name):  # the artifact flags only — the creation-time domain (round 16) is
        caps = method_capabilities(name)  # pinned by tests/test_method_capabilities.py
        return (caps.config_bundle, caps.user_source, caps.runtime_versions, caps.candidate_mint)

    assert artifact("zip_runtime") == (True, False, True, True)
    assert artifact("harness") == (True, False, True, True)
    assert artifact("studio") == (False, True, True, True)
    assert artifact("byoc") == (False, True, True, False)
    # an unregistered method (the Global `container`, a typo) supports nothing
    assert method_capabilities("container") == MethodCapabilities()
    assert method_capabilities(None) == MethodCapabilities()


# ─── experiment eligibility matrix ────────────────────────────────────────────
# ─── canary eligibility matrix ────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (_row("harness"), ELIGIBLE_CANARY),
        (_row("zip_runtime"), ELIGIBLE_CANARY),
        (_row("studio", spec={"code": "canvas"}), ELIGIBLE_CANARY),
        (_row("byoc"), "custom-source-unverified"),
        (_row("container"), "not-runtime"),
        (_row("harness", system_key="hr-helpdesk"), "system-managed"),
        (_row("harness", status="failed"), "not-active"),
        (_row("harness", spec={"protocol": "a2a"}), "a2a"),
        (_row("harness", arn=None), "no-runtime-arn"),
        (_row("harness", arn="arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:harness/x"),
         "no-runtime-arn"),
    ],
    ids=lambda v: v if isinstance(v, str) else (
        f"{v.method}:{v.status}:{v.spec.get('protocol', 'http')}:{'arn' if v.arn else 'noarn'}"
        if isinstance(v, SimpleNamespace) else "eligible"),
)
def test_canary_capability_matrix(row, expected):
    cap = capability.canary_capability(row)
    if isinstance(expected, dict):
        assert cap == expected
    else:
        assert cap["eligible"] is False
        assert cap["reason_code"] == expected
        assert cap["reason"]


def test_old_method_name_codes_are_gone():
    """`not-http-runtime` named a method, not a capability; nothing emits it any more."""
    assert "not-http-runtime" not in CAPABILITY_SRC.read_text(encoding="utf-8")


# ─── reason-code i18n coverage (N12-obs35) ────────────────────────────────────
_CODE_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _reason_codes(*function_names: str) -> set[str]:
    """Every `reason_code` literal the named functions in optimization/capability.py can
    emit: dict entries `"reason_code": "<code>"`."""
    tree = ast.parse(CAPABILITY_SRC.read_text(encoding="utf-8"))
    functions = {
        node.name: node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in function_names
    }
    assert set(functions) == set(function_names), "capability function renamed?"
    codes: set[str] = set()
    for fn in functions.values():
        for node in ast.walk(fn):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if (
                        isinstance(key, ast.Constant) and key.value == "reason_code"
                        and isinstance(value, ast.Constant) and isinstance(value.value, str)
                    ):
                        codes.add(value.value)
    assert codes and all(_CODE_RE.match(c) for c in codes), codes
    return codes


def _locale_reasons(locale: str, page: str) -> dict[str, str]:
    data = json.loads((LOCALES / locale / "common.json").read_text(encoding="utf-8"))
    return dict(data[page]["reason"])


@pytest.mark.parametrize(
    ("page", "functions"),
    [
        ("canaryPage", ("canary_capability",)),
    ],
)
@pytest.mark.parametrize("locale", ["en", "zh-CN"])
def test_every_reason_code_has_a_console_string(page, functions, locale):
    """N12-obs35: the dropdown rendered the raw key `expPage.reason.system-managed` because
    the locale lacked it — every code the backend can emit must have a string in the
    page's namespace in BOTH locales (plus the `unknown` fallback the console uses when a
    server sends no code and no text)."""
    codes = _reason_codes(*functions)
    reasons = _locale_reasons(locale, page)
    missing = sorted(code for code in codes if not reasons.get(code))
    assert not missing, f"{locale} {page}.reason lacks {missing}"
    assert reasons.get("unknown")


def test_expected_codes_are_emitted():
    """The matrix above pins the behaviour; this pins the vocabulary the console ships."""
    assert _reason_codes("canary_capability") == {
        "system-managed", "not-active", "not-runtime", "custom-source-unverified", "a2a",
        "no-runtime-arn",
    }


# ─── runner paths for a harness agent ─────────────────────────────────────────
def _persist(agent: Agent) -> str:
    db = SessionLocal()
    try:
        db.add(agent)
        db.commit()
        return agent.id
    finally:
        db.close()


def _harness_agent(name: str = "hr-helpdesk", **kw) -> Agent:
    return Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name=name, method="harness", status="active",
        arn=RUNTIME_ARN, resource_id=f"{name}-abcdefghij", version="3",
        spec={"name": name, "method": "harness", "system_prompt": "You answer HR questions.",
              **kw},
    )


def test_canary_start_admits_a_harness_agent(client, monkeypatch):
    """N12-obs34: POST /api/runtime-canaries for a CN harness agent creates the ledger row
    with an eligible capability snapshot and the edited spec kept a plain harness spec."""
    agent_id = _persist(_harness_agent())
    control = MagicMock()
    control.get_agent_runtime.side_effect = lambda agentRuntimeId: {
        "agentRuntimeName": f"runtime-{agentRuntimeId}"
    }
    monkeypatch.setattr(canary_svc, "control_client", lambda _ws=None: control)

    res = client.post(
        "/api/runtime-canaries",
        json={"agent_id": agent_id, "candidate": {"system_prompt": "Be brief."}},
    )
    assert res.status_code == 201, res.json()
    body = res.json()
    meta = body["artifacts"]["agent_meta"]
    assert meta["canary_capability"] == ELIGIBLE_CANARY
    assert meta["runtime_name"] == "runtime-hr-helpdesk-abcdefghij"
    edited = body["artifacts"]["edited_spec"]
    assert edited["method"] == "harness"
    assert edited["system_prompt"] == "Be brief."
    assert edited.get("code_bundle") is None  # the graft path is for converted exports


def test_canary_start_still_refuses_byoc(client):
    agent_id = _persist(Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name="own-code", method="byoc", status="active",
        arn=RUNTIME_ARN, resource_id="own-code-abcdefghij", spec={"protocol": "http"},
    ))
    res = client.post(
        "/api/runtime-canaries",
        json={"agent_id": agent_id, "candidate": {"system_prompt": "p"}},
    )
    assert res.status_code == 400
    assert res.json()["code"] == "canary.agent_unsupported"
    assert res.json()["detail"]["canary_capability"]["reason_code"] == (
        "custom-source-unverified")


def _fake_pip(_cmd, **_kwargs):
    """Both subprocesses build_zip drives: the uv resolve leaves a lockfile at `-o`, the
    pip install is a no-op."""
    if "compile" in _cmd:
        Path(_cmd[_cmd.index("-o") + 1]).write_text(
            "probe-pkg==1.0.0 \\\n    --hash=sha256:" + "e" * 64 + "\n", encoding="utf-8",
        )
    return SimpleNamespace(returncode=0, stderr="")


def _control(v_current: str, v_candidate: str) -> MagicMock:
    control = MagicMock()
    control.get_agent_runtime.return_value = {
        "agentRuntimeVersion": v_current, "status": "READY",
        "agentRuntimeArn": RUNTIME_ARN, "agentRuntimeName": "hr_helpdesk",
        "roleArn": "arn:aws-cn:iam::111122223333:role/hr-helpdesk-runtime",
    }
    control.update_agent_runtime.return_value = {
        "agentRuntimeVersion": v_candidate, "status": "READY", "agentRuntimeArn": RUNTIME_ARN,
    }
    return control


def test_mint_candidate_renders_the_harness_bundle(monkeypatch, tmp_path):
    """N12-obs34: a harness candidate is the self-built Harness tree re-rendered from the
    edited spec (same renderer as a deploy), zipped and published as a new version of the
    SAME runtime — with the platform keys production carries, never a ledger write."""
    monkeypatch.setattr(harness_local, "TEMPLATE_DIR", FIXTURE_TEMPLATE)
    workspace = ws_ctx(
        {"artifacts_bucket": "bkt", "execution_role_arn": "arn:role/exec"},
        region="cn-northwest-1",
    )
    agent_id = _persist(_harness_agent())
    control = _control("3", "4")
    uploads: list[tuple[str, str, str]] = []
    edited = AgentSpec(name="hr-helpdesk", method="harness", system_prompt="Be brief.")

    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        v_current, v_candidate, s3_key = infra.mint_candidate_version(
            agent=agent, edited_spec=edited, control_client=control, workspace=workspace,
            canary_id="can789", pip_runner=_fake_pip,
            uploader=lambda local, bucket, key: uploads.append((local, bucket, key)),
            build_root=tmp_path,
        )
    finally:
        db.close()

    assert (v_current, v_candidate) == ("3", "4")
    assert s3_key == "agents/hr-helpdesk/canary/can789-candidate.zip"
    assert [(b, k) for _, b, k in uploads] == [("bkt", s3_key)]
    with zipfile.ZipFile(uploads[0][0]) as zf:
        names = set(zf.namelist())
        main_py = zf.read("main.py").decode("utf-8")
    # the whole rendered tree, not just an entrypoint: the Harness is multi-file
    assert {"main.py", "model/load.py", "memory/session.py", "launchpad_memory.py"} <= names
    assert GRAFT_START in main_py  # the candidate consumes config bundles like production
    assert "Be brief." in main_py  # the edit is the rendered default prompt
    up = control.update_agent_runtime.call_args.kwargs
    assert up["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"] == {
        "bucket": "bkt", "prefix": s3_key}
    # the candidate keeps production's role (read from the live runtime)
    assert up["roleArn"] == "arn:aws-cn:iam::111122223333:role/hr-helpdesk-runtime"
    env = up["environmentVariables"]
    assert env[ENV_AGENT_ID] == agent_id and env[ENV_WORKLOAD] == "hr-helpdesk-abcdefghij"
    # no ledger write: production stays on v_current until the canary completes
    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        assert row.version == "3" and row.spec["system_prompt"] == "You answer HR questions."
    finally:
        db.close()


def test_mint_candidate_zip_runtime_keeps_the_platform_identity_keys(monkeypatch, tmp_path):
    """Regression for the environment fix: a zip_runtime candidate (and after `complete`,
    production) must not lose LAUNCHPAD_AGENT_ID / LAUNCHPAD_WORKLOAD_NAME."""
    workspace = ws_ctx({"artifacts_bucket": "bkt", "execution_role_arn": "arn:role/exec"})
    agent_id = _persist(Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name="zipper", method="zip_runtime",
        status="active", arn=RUNTIME_ARN, resource_id="zipper-abcdefghij", version="1",
        spec={"name": "zipper", "method": "zip_runtime", "system_prompt": "orig"},
    ))
    control = _control("1", "2")
    db = SessionLocal()
    try:
        infra.mint_candidate_version(
            agent=db.get(Agent, agent_id),
            edited_spec=AgentSpec(name="zipper", method="zip_runtime", system_prompt="new"),
            control_client=control, workspace=workspace, canary_id="can1",
            pip_runner=_fake_pip, uploader=lambda *_a: None, build_root=tmp_path,
        )
    finally:
        db.close()
    env = control.update_agent_runtime.call_args.kwargs["environmentVariables"]
    assert env[ENV_AGENT_ID] == agent_id and env[ENV_WORKLOAD] == "zipper-abcdefghij"


def test_mint_candidate_refuses_methods_without_candidate_mint(tmp_path):
    workspace = ws_ctx({"artifacts_bucket": "bkt"})
    spec = AgentSpec(name="own-code", method="byoc", system_prompt="p",
                     byoc={"artifact_kind": "code_zip", "upload_id": "u1"})
    with pytest.raises(NotImplementedError):
        infra.mint_candidate_version(
            agent=SimpleNamespace(id="a", name="own-code", resource_id="r"), edited_spec=spec,
            control_client=MagicMock(), workspace=workspace, canary_id="c",
            pip_runner=_fake_pip, uploader=lambda *_a: None, build_root=tmp_path,
        )
