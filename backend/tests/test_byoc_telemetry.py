"""N17-obs410: a BYOC code_zip starts through the ADOT ``opentelemetry-instrument`` launcher
when the BUILT bundle carries it (requirements.txt-resolved or vendored); otherwise it starts
plain — never failing on an absent launcher — and the deploy says "no telemetry"."""

import io
import zipfile

from app.deployer import byoc as byoc_dep
from app.schemas.agent import AgentSpec
from app.services import byoc_uploads
from tests.conftest import ws_ctx
from tests.test_byoc import (
    RESOURCES,
    SDK_MAIN,
    StubRuntimeControl,
    StubS3,
    _byoc_spec,
    _client_router,
    _get_agent,
    _mk_agent,
    _seed_upload,
    _stage_ctx,
    zip_bytes,
)

DISTRO = {
    "aws_opentelemetry_distro/__init__.py": b"",
    "aws_opentelemetry_distro-0.12.0.dist-info/METADATA": b"Name: aws-opentelemetry-distro\n",
    "opentelemetry/instrumentation/auto_instrumentation/__init__.py": b"",
}


def _detect(files: dict[str, bytes], tmp_path) -> str:
    path = tmp_path / "u.zip"
    path.write_bytes(zip_bytes(files))
    return byoc_uploads.validate_and_detect(path)["detected"]["telemetry"]["adot"]


def test_upload_reports_where_the_launcher_would_come_from(tmp_path):
    assert _detect({"main.py": SDK_MAIN, **DISTRO}, tmp_path) == "vendored"
    assert _detect({"main.py": SDK_MAIN,
                    "requirements.txt": b"strands-agents\naws-opentelemetry-distro>=0.10,<1\n"},
                   tmp_path) == "requirements"
    assert _detect({"main.py": SDK_MAIN, "pyproject.toml":
                    b'[project]\ndependencies = ["aws-opentelemetry-distro>=0.10"]\n'},
                   tmp_path) == "pyproject_only"
    assert _detect({"main.py": SDK_MAIN,
                    "requirements.txt": b"# aws-opentelemetry-distro\nboto3\n"},
                   tmp_path) == "absent"


def _package_then_deploy(monkeypatch, files: dict[str, bytes], **byoc_over):
    s3 = StubS3()
    _client_router(monkeypatch, {"s3": s3})
    _seed_upload(s3, "u1", files)
    stub = StubRuntimeControl()
    monkeypatch.setattr(byoc_dep, "control_client", lambda _ws=None: stub)
    spec = AgentSpec(**_byoc_spec(byoc={"artifact_kind": "code_zip", "upload_id": "u1",
                                        **byoc_over}))
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES))
    package = byoc_dep._stage_package(ctx, _get_agent(agent_id))
    ctx.scratch["execution_role_arn"] = "arn:agent-role"
    deploy = byoc_dep._stage_deploy(ctx, _get_agent(agent_id))
    entry = stub.created_with["agentRuntimeArtifact"]["codeConfiguration"]["entryPoint"]
    return package, deploy, entry, _get_agent(agent_id).spec["byoc"]["telemetry"], s3


def test_a_vendored_distro_starts_through_the_launcher(monkeypatch):
    package, deploy, entry, telemetry, _ = _package_then_deploy(
        monkeypatch, {"main.py": SDK_MAIN, **DISTRO}, install_requirements=False)
    assert telemetry == {"instrumented": True, "reason": ""}
    assert entry == ["opentelemetry-instrument", "main.py"]
    assert "ADOT ✓" in package.detail
    assert byoc_dep.NO_TELEMETRY_WARNING not in deploy.detail


def test_a_requirements_resolved_distro_starts_through_the_launcher(monkeypatch):
    def resolve(src_root, *_args, **_kw):  # the hashed install lands the distro at the root
        for rel, data in DISTRO.items():
            (src_root / rel).parent.mkdir(parents=True, exist_ok=True)
            (src_root / rel).write_bytes(data)
        return 3

    monkeypatch.setattr(byoc_dep, "resolve_requirements_into", resolve)
    _, _, entry, telemetry, _ = _package_then_deploy(
        monkeypatch, {"main.py": SDK_MAIN,
                      "requirements.txt": b"aws-opentelemetry-distro>=0.10,<1\n"})
    assert telemetry["instrumented"] is True
    assert entry == ["opentelemetry-instrument", "main.py"]


def test_without_the_distro_the_start_is_plain_and_the_deploy_warns(monkeypatch):
    package, deploy, entry, telemetry, _ = _package_then_deploy(
        monkeypatch, {"main.py": SDK_MAIN})
    # an absent launcher never fails the start: the runtime is created READY, plain python
    assert entry == ["main.py"]
    assert deploy.detail.startswith("READY")
    assert byoc_dep.NO_TELEMETRY_WARNING in package.detail
    assert byoc_dep.NO_TELEMETRY_WARNING in deploy.detail
    assert telemetry["instrumented"] is False
    assert "does not install aws-opentelemetry-distro" in telemetry["reason"]


def test_a_distro_without_its_launcher_is_not_instrumented(monkeypatch):
    files = {k: v for k, v in DISTRO.items() if "auto_instrumentation" not in k}
    _, _, entry, telemetry, _ = _package_then_deploy(
        monkeypatch, {"main.py": SDK_MAIN, **files}, install_requirements=False)
    assert entry == ["main.py"] and "launcher" in telemetry["reason"]


def test_a_declared_but_uninstalled_distro_names_the_reason(monkeypatch):
    _, _, entry, telemetry, _ = _package_then_deploy(
        monkeypatch, {"main.py": SDK_MAIN,
                      "requirements.txt": b"aws-opentelemetry-distro>=0.10,<1\n"},
        install_requirements=False)
    assert entry == ["main.py"] and "install_requirements is off" in telemetry["reason"]
    _, _, entry, telemetry, _ = _package_then_deploy(
        monkeypatch, {"main.py": SDK_MAIN, "pyproject.toml":
                      b'dependencies = ["aws-opentelemetry-distro"]\n'})
    assert entry == ["main.py"] and "pyproject.toml" in telemetry["reason"]


def test_a_client_sent_verdict_is_overwritten_by_the_package_stage(monkeypatch):
    _, _, entry, telemetry, s3 = _package_then_deploy(
        monkeypatch, {"main.py": SDK_MAIN},
        telemetry={"instrumented": True, "reason": "trust me"})
    assert entry == ["main.py"] and telemetry["instrumented"] is False
    with zipfile.ZipFile(io.BytesIO(s3.objects[("bkt", "agents/byoc-agent/byoc_package.zip")])) \
            as zf:
        assert "main.py" in zf.namelist()
