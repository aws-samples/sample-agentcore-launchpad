"""s3BucketMount compatibility (task 11-23-s3mount-p0-compat-probe): the member kinds,
the PUBLIC rules, the live view, the per-session session-id guard and the mount-failure
mapping. Stub clients; no AWS call is made."""

import botocore.session
import pytest
from botocore.exceptions import ClientError

from app.core.errors import AppError
from app.deployer import filesystem
from app.deployer.filesystem import runtime_update_mounts
from app.routers.public_api import public_stream_event
from app.services import chat as chat_service
from app.services import invoke as invoke_service
from app.services import invoke_session
from app.services.agentcore import harness as hc
from app.services.agentcore import runtime as rt
from app.services.agentcore.filesystem_kinds import (
    member_kind,
    needs_vpc,
    per_session_prefix,
)
from app.services.invoke_session import assert_session_id_compatible, session_id_fits_mount
from app.services.mount_failure import MOUNT_FAILED, mount_failure

SESSION = {"sessionStorage": {"mountPath": "/mnt/workspace"}}
BUCKET = {"s3BucketMount": {"bucket": "acme-lake", "mountPath": "/mnt/lake",
                            "prefix": "curated/"}}
PER_SESSION = {"s3BucketMount": {"bucket": "acme-lake", "mountPath": "/mnt/scratch",
                                 "prefix": "sessions/{runtimeSessionId}/"}}
S3_FILES = {"s3FilesAccessPoint": {
    "accessPointArn": "arn:aws:s3files:us-west-2:111122223333:file-system/fs-1/access-point/ap-1",
    "mountPath": "/mnt/data"}}
EFS = {"efsAccessPoint": {
    "accessPointArn": "arn:aws:elasticfilesystem:us-west-2:111122223333:access-point/fsap-1",
    "mountPath": "/mnt/efs"}}
VPC = {"subnets": ["subnet-a"], "security_groups": ["sg-a"]}


# ── the SDK carries the new union member ─────────────────────────────────────


def test_the_installed_sdk_knows_s3_bucket_mount():
    """botocore < 1.44.6 rejects the entry client-side; guards a lock regression."""
    model = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
    members = model.shape_for("FilesystemConfiguration").members
    assert "s3BucketMount" in members
    assert {"bucket", "mountPath", "prefix", "s3BucketOwner", "s3BucketRegion"} <= set(
        model.shape_for("S3BucketMountConfiguration").members)


# ── classification ───────────────────────────────────────────────────────────


def test_member_kind_needs_vpc_and_per_session_prefix():
    assert [member_kind(e) for e in (SESSION, BUCKET, S3_FILES, EFS, {})] == [
        "sessionStorage", "s3BucketMount", "s3FilesAccessPoint", "efsAccessPoint", ""]
    assert [needs_vpc(e) for e in (SESSION, BUCKET, S3_FILES, EFS)] == [False, False, True, True]
    assert [per_session_prefix(e) for e in (SESSION, BUCKET, PER_SESSION, S3_FILES)] == [
        False, False, True, False]
    # no prefix = the whole bucket
    assert per_session_prefix({"s3BucketMount": {"bucket": "b", "mountPath": "/mnt/b"}}) is False


def test_deployer_filesystem_reexports_the_helpers():
    assert filesystem.needs_vpc is needs_vpc and filesystem.member_kind is member_kind


# ── R0.2: the create guard ───────────────────────────────────────────────────


class RuntimeStub:
    def __init__(self):
        self.created_with = self.updated_with = None

    def create_agent_runtime(self, **kwargs):
        self.created_with = kwargs
        return {"agentRuntimeId": "rt-1", "agentRuntimeArn": "arn:rt-1",
                "agentRuntimeVersion": "1", "status": "CREATING"}

    def update_agent_runtime(self, **kwargs):
        self.updated_with = kwargs
        return {"agentRuntimeId": "rt-1", "agentRuntimeVersion": "2", "status": "UPDATING"}

    def get_agent_runtime(self, **_kw):
        return {"agentRuntimeArn": "arn:rt-1", "agentRuntimeVersion": "1", "status": "READY"}


def test_a_bucket_mount_is_not_refused_on_public():
    stub = RuntimeStub()
    rt.create_code_runtime(stub, runtime_name="x", s3_bucket="b", s3_key="k", role_arn="arn:r",
                           filesystem_configurations=[SESSION, BUCKET, PER_SESSION])
    assert stub.created_with["filesystemConfigurations"] == [SESSION, BUCKET, PER_SESSION]
    assert stub.created_with["networkConfiguration"] == {"networkMode": "PUBLIC"}


@pytest.mark.parametrize("nfs", [S3_FILES, EFS])
def test_nfs_mounts_are_still_refused_on_public(nfs):
    stub = RuntimeStub()
    with pytest.raises(ValueError, match="need the runtime in a VPC"):
        rt.create_code_runtime(stub, runtime_name="x", s3_bucket="b", s3_key="k",
                               role_arn="arn:r", filesystem_configurations=[SESSION, BUCKET, nfs])
    assert stub.created_with is None
    # with a VPC, both are fine together
    rt.create_code_runtime(stub, runtime_name="x", s3_bucket="b", s3_key="k", role_arn="arn:r",
                           filesystem_configurations=[SESSION, BUCKET, nfs], vpc=VPC)
    assert stub.created_with["networkConfiguration"]["networkMode"] == "VPC"


# ── R0.3: the re-publish merge ───────────────────────────────────────────────


class LiveRuntime:
    def __init__(self, live):
        self.live = live

    def get_agent_runtime(self, agentRuntimeId):  # noqa: N803
        return {"agentRuntimeId": agentRuntimeId, "filesystemConfigurations": self.live}


@pytest.fixture
def platform_session_only(monkeypatch):
    monkeypatch.setattr(filesystem, "filesystem_configurations",
                        lambda _spec, _agent_id=None: [SESSION])
    return object()


def test_a_public_republish_keeps_an_external_bucket_mount_and_detaches_nfs(
        platform_session_only):
    logs: list[str] = []
    control = LiveRuntime([SESSION, BUCKET, S3_FILES, EFS])
    out = runtime_update_mounts(control, "rt-1", platform_session_only, managed_paths=set(),
                                vpc=None, log=logs.append)
    assert out == [SESSION, BUCKET]
    assert any("/mnt/data" in line and "S3 Files / EFS need a VPC" in line for line in logs)
    assert any("/mnt/efs" in line and "detached" in line for line in logs)
    assert any("/mnt/lake" in line and "kept" in line for line in logs)
    assert not any("/mnt/lake" in line and "detached" in line for line in logs)


def test_a_vpc_republish_keeps_all_of_them(platform_session_only):
    control = LiveRuntime([SESSION, BUCKET, S3_FILES])
    out = runtime_update_mounts(control, "rt-1", platform_session_only, managed_paths=set(),
                                vpc=VPC, log=lambda _l: None)
    assert out == [SESSION, BUCKET, S3_FILES]


def test_a_bucket_mount_the_platform_set_is_dropped_when_the_spec_dropped_it(
        platform_session_only):
    out = runtime_update_mounts(LiveRuntime([SESSION, BUCKET]), "rt-1", platform_session_only,
                                managed_paths={"/mnt/lake"}, vpc=None, log=lambda _l: None)
    assert out == [SESSION]


def test_a_canary_candidate_on_public_keeps_a_bucket_mount_only(monkeypatch):
    from app.optimization import canary_infra

    monkeypatch.setattr(canary_infra, "filesystem_configurations",
                        lambda _spec, _agent_id=None: [SESSION])
    monkeypatch.setattr(canary_infra, "spec_vpc", lambda *_a, **_kw: None)
    agent = type("A", (), {"id": "a1", "spec": {}, "workspace_id": "default"})()
    logs: list[str] = []
    merged = canary_infra._candidate_mounts(
        {"filesystemConfigurations": [SESSION, BUCKET, S3_FILES]}, object(), agent, logs.append)
    assert merged == [SESSION, BUCKET]
    assert any("S3 Files / EFS" in line for line in logs)


# ── R0.5: the Harness keeps it too ───────────────────────────────────────────


def test_harness_update_keeps_an_external_bucket_mount():
    live = {"environment": {"agentCoreRuntimeEnvironment": {
        "filesystemConfigurations": [SESSION, BUCKET]}}}
    env = hc.environment_for_update(live, [SESSION])
    assert env["agentCoreRuntimeEnvironment"]["filesystemConfigurations"] == [SESSION, BUCKET]
    # PUBLIC network does not detach it either (network is a separate field)
    env = hc.environment_for_update(live, [SESSION], network=None)
    assert env["agentCoreRuntimeEnvironment"]["filesystemConfigurations"] == [SESSION, BUCKET]
    # ... and one the platform set earlier is detached when the spec dropped it
    env = hc.environment_for_update(live, [SESSION], managed_paths={"/mnt/lake"})
    assert env["agentCoreRuntimeEnvironment"]["filesystemConfigurations"] == [SESSION]


# ── R0.6: the session-id guard ───────────────────────────────────────────────

ULID_FEISHU = "01JABCDEFGHJKMNPQRSTVWXYZ0#feishu#oc_123456"
HEX64 = "a1" * 32


def test_session_id_rule():
    assert session_id_fits_mount(HEX64)
    assert session_id_fits_mount(hc.new_session_id())
    assert session_id_fits_mount(None) and session_id_fits_mount("")
    assert session_id_fits_mount("A" * 33) and session_id_fits_mount("a-b_" * 8 + "c")
    assert session_id_fits_mount("a" * 256)
    assert not session_id_fits_mount(ULID_FEISHU)
    assert not session_id_fits_mount("abc#feishu#" + "x" * 30)
    assert not session_id_fits_mount("a" * 32)       # AgentCore needs >= 33
    assert not session_id_fits_mount("a" * 257)
    assert not session_id_fits_mount("-" + "a" * 40)  # must start alphanumeric
    assert not session_id_fits_mount("a" * 40 + ".")


def _agent(monkeypatch, entries):
    monkeypatch.setattr(filesystem, "filesystem_configurations",
                        lambda _spec, _agent_id=None: entries)
    from app.schemas.agent import AgentSpec

    spec = AgentSpec(name="plain", method="zip_runtime", system_prompt="x").model_dump()
    return type("A", (), {"id": "a1", "spec": spec})()


def test_the_guard_refuses_an_incompatible_id_for_a_per_session_mount(monkeypatch):
    agent = _agent(monkeypatch, [SESSION, PER_SESSION])
    with pytest.raises(AppError) as err:
        assert_session_id_compatible(agent, "abc#feishu#" + "x" * 30)
    assert err.value.code == "invoke.session_id_incompatible" and err.value.status_code == 400
    assert "33-256" in err.value.message
    assert_session_id_compatible(agent, HEX64)
    assert_session_id_compatible(agent, None)


def test_no_guard_without_a_per_session_mount(monkeypatch):
    for entries in ([SESSION], [SESSION, BUCKET, S3_FILES]):
        assert_session_id_compatible(_agent(monkeypatch, entries), "abc#feishu#" + "x" * 30)


def test_the_guard_never_breaks_an_agent_whose_spec_does_not_parse():
    agent = type("A", (), {"spec": {"method": "no-such-method"}})()
    assert_session_id_compatible(agent, ULID_FEISHU)
    assert_session_id_compatible(type("A", (), {"spec": None})(), ULID_FEISHU)


def test_a_real_spec_has_no_per_session_mount_yet():
    # P0: nothing in the schema produces one, so every ordinary agent passes any id
    from app.schemas.agent import AgentSpec

    spec = AgentSpec(name="plain", method="zip_runtime", system_prompt="x").model_dump()
    assert not invoke_session.has_per_session_mount(type("A", (), {"spec": spec})())


def test_the_shared_invoke_chain_runs_the_guard_before_dispatch(monkeypatch):
    agent = _agent(monkeypatch, [PER_SESSION])
    agent.system_key = None
    monkeypatch.setattr(invoke_service, "require_invoke_capability", lambda _a: None)
    monkeypatch.setattr("app.services.project_quotas.assert_agent_budget", lambda _a: None)
    monkeypatch.setattr(invoke_service.guardrail, "guardrail_config", lambda _s: None)
    monkeypatch.setattr(invoke_service.answer_rules, "match_for_agent", lambda *_a: None)
    monkeypatch.setattr(invoke_service, "_agent_workspace",
                        lambda *_a: pytest.fail("dispatch reached AWS wiring"))
    with pytest.raises(AppError) as err:
        invoke_service.invoke_agent_text(agent, "hi", session_id=ULID_FEISHU)
    assert err.value.code == "invoke.session_id_incompatible"
    with pytest.raises(AppError) as err:
        list(invoke_service.invoke_agent_events(agent, "hi", session_id=ULID_FEISHU))
    assert err.value.code == "invoke.session_id_incompatible"


def test_chat_stream_ends_with_the_named_error_event(monkeypatch):
    agent = _agent(monkeypatch, [PER_SESSION])
    agent.name, agent.method, agent.resource_id = "n", "zip_runtime", "r"
    agent.workspace_id, agent.system_key = "default", None
    monkeypatch.setattr(chat_service.guardrail, "guardrail_config", lambda _s: None)
    monkeypatch.setattr(chat_service.inbound_auth_service, "is_jwt_mode", lambda _a: False)
    monkeypatch.setattr(chat_service, "invoke_agent_events",
                        lambda *_a, **_kw: pytest.fail("the model was reached"))
    events = list(chat_service.chat_stream(agent, "hi", session_id=ULID_FEISHU,
                                           workspace=None, status_events=True))
    assert [e["event"] for e in events] == ["meta", "error"]
    assert events[-1]["data"]["code"] == "invoke.session_id_incompatible"
    # /v1 keeps the code and message
    public = public_stream_event(events[-1])["data"]
    assert public["code"] == "invoke.session_id_incompatible"


# ── R0.7: a failed mount is named ────────────────────────────────────────────


def _mount_424(message="S3 mount failed: access denied") -> ClientError:
    return ClientError({"Error": {"Code": "RuntimeClientError", "Message": message},
                        "ResponseMetadata": {"HTTPStatusCode": 424}}, "InvokeAgentRuntime")


def test_mount_failure_maps_a_424_with_awss_reason():
    err = mount_failure(_mount_424())
    assert err is not None and err.code == MOUNT_FAILED and err.status_code == 424
    assert err.detail == {"reason": "access denied", "retryable": False}
    assert "access denied" in err.message
    # the other documented reasons
    assert mount_failure(_mount_424("S3 mount failed: the S3 bucket was not found")
                         ).detail["reason"] == "the S3 bucket was not found"
    # a 424 without the phrase is AgentCore's generic RuntimeClientError (any agent
    # container failure) and keeps its existing handling
    assert mount_failure(_mount_424("Received error (500) from runtime")) is None


def test_mount_failure_matches_the_text_alone_and_ignores_other_errors():
    streamed = RuntimeError("runtime client error: S3 mount failed: the mount configuration "
                            "was rejected")
    assert mount_failure(streamed).detail["reason"] == "the mount configuration was rejected"
    bearer = rt.RuntimeBearerHttpError(424, "bearer invoke returned HTTP 424: S3 mount "
                                       "failed: access denied")
    assert mount_failure(bearer).code == MOUNT_FAILED
    assert mount_failure(rt.RuntimeBearerHttpError(424, "bearer invoke returned HTTP 424")
                         ) is None
    throttled = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"},
                             "ResponseMetadata": {"HTTPStatusCode": 429}}, "InvokeAgentRuntime")
    for other in (throttled, ValueError("bug"), rt.RuntimeBearerHttpError(500, "boom")):
        assert mount_failure(other) is None
    named = mount_failure(_mount_424())
    assert mount_failure(named) is named
    assert mount_failure(AppError("agent.upstream_error", "x")) is None


def test_the_sync_routes_answer_it_on_console_and_v1():
    for public in (False, True):
        err = invoke_service.upstream_invoke_error(_mount_424(), public=public)
        assert err.code == MOUNT_FAILED and err.status_code == 424
        assert err.detail["reason"] == "access denied"


def test_the_chat_stream_emits_agent_mount_failed_without_a_retry(monkeypatch):
    from app.routers.chat import retry_hint

    agent = type("A", (), {"id": "a1", "name": "n", "method": "zip_runtime", "spec": {},
                           "resource_id": "r", "workspace_id": "default",
                           "system_key": None})()

    def boom(*_a, **_kw):
        raise _mount_424()
        yield  # pragma: no cover

    monkeypatch.setattr(chat_service, "invoke_agent_events", boom)
    monkeypatch.setattr(chat_service.guardrail, "guardrail_config", lambda _s: None)
    monkeypatch.setattr(chat_service.inbound_auth_service, "is_jwt_mode", lambda _a: False)
    events = list(chat_service.chat_stream(agent, "hi", session_id=HEX64, workspace=None,
                                           status_events=True))
    data = [e for e in events if e["event"] == "error"][0]["data"]
    assert data["code"] == MOUNT_FAILED and data["transient"] is False
    assert data["detail"]["reason"] == "access denied"
    assert retry_hint(data, produced=False) == {"category": "config", "manual": False,
                                                "auto": False}
    public = public_stream_event({"event": "error", "data": data})["data"]
    assert public["code"] == MOUNT_FAILED and public["detail"]["reason"] == "access denied"
