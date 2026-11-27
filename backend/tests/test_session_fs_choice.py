"""Managed session storage and the session output folder (S3 bucket): independent, either,
both or neither may be on (tasks 11-27-session-fs-choice, 11-27-session-fs-both). Turning one
off on a re-publish rides the existing ownership merge — a live member the platform set
earlier is detached, the other is kept/sent — and every deployer that merges (the three main
ones, the canary candidate, the Harness) must see the paths an earlier publish set
(`published_mount_paths`). No AWS call is made."""

import json

import pytest
from pydantic import ValidationError

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer import mounts
from app.deployer.filesystem import (
    filesystem_configurations,
    merge_live,
    runtime_update_mounts,
)
from app.models.ledger import SpecSnapshot
from app.optimization import canary_infra
from app.schemas.agent import AgentSpec, FilesystemConfig
from app.services.agentcore import harness as hc
from tests.test_agent_files import AGENT, BUCKET, STAMPED, V2

SESSION = {"sessionStorage": {"mountPath": "/mnt/workspace"}}
OUTPUTS = {"s3BucketMount": {
    "bucket": BUCKET, "mountPath": "/mnt/outputs",
    "prefix": f"projects/proj-test/agents/{AGENT}/sessions/{{runtimeSessionId}}/"}}
EXTERNAL = {"s3BucketMount": {"bucket": "acme-lake", "mountPath": "/mnt/lake"}}


def _spec(*, outputs: bool, session: bool | None = None,
          method: str = "zip_runtime") -> AgentSpec:
    """`session` defaults to the opposite of `outputs` (the either/or switch); pass it to
    turn both on or both off."""
    session = (not outputs) if session is None else session
    fs: dict = {"outputs": STAMPED} if outputs else {}
    if not session:
        fs["session_storage"] = None
    body = {"name": "switch-agent", "method": method, "system_prompt": "p", "filesystem": fs}
    if method != "harness":
        body["runtime_config"] = V2
    return AgentSpec.model_validate(body)


def _published(*specs: AgentSpec) -> set[str]:
    """`published_mount_paths` over a real ledger: one snapshot per publish."""
    from app.models.ledger import Agent

    db = SessionLocal()
    try:
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="switch-agent",
                      method="zip_runtime", spec={})
        db.add(agent)
        db.commit()
        for seq, spec in enumerate(specs, 1):
            db.add(SpecSnapshot(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id,
                                seq=seq, spec=spec.model_dump(mode="json")))
        db.commit()
        return mounts.published_mount_paths(db, agent.id)
    finally:
        db.close()


# ── the spec rule ────────────────────────────────────────────────────────────


def test_a_default_filesystem_keeps_the_golden_fixture_shape():
    """Nothing was renamed or re-defaulted: the 11 Strands fixtures render from specs
    whose filesystem is the default."""
    dumped = FilesystemConfig().model_dump(mode="json")
    assert dumped["session_storage"] == {"mount_path": "/mnt/workspace"}
    assert dumped["outputs"] is None
    assert AgentSpec.model_validate(
        {"name": "a-1", "method": "zip_runtime", "system_prompt": "p"}
    ).filesystem.session_storage.mount_path == "/mnt/workspace"
    # explicit null stays OFF; outputs alone no longer touches session storage
    assert FilesystemConfig.model_validate({"session_storage": None}).session_storage is None
    only_outputs = FilesystemConfig.model_validate({"outputs": {}})
    assert only_outputs.session_storage is not None and only_outputs.outputs is not None


def test_both_stores_may_be_on_and_each_takes_one_of_the_five_entries():
    both = FilesystemConfig.model_validate({"session_storage": {}, "outputs": {}})
    assert both.session_storage.mount_path == "/mnt/workspace"
    assert both.outputs.mount_path == "/mnt/outputs"
    refs = [{"storage_id": f"store{i}"} for i in range(5)]
    # session storage takes one of the five, the folder another: three mounts fit, four do not
    assert FilesystemConfig.model_validate(
        {"session_storage": {}, "outputs": {}, "mounts": refs[:3]})
    with pytest.raises(ValidationError, match="at most 4"):
        FilesystemConfig.model_validate(
            {"session_storage": {}, "outputs": {}, "mounts": refs[:4]})


def test_both_on_renders_both_members():
    spec = _spec(outputs=True, session=True)
    assert spec.filesystem.session_storage is not None and spec.filesystem.outputs is not None
    assert filesystem_configurations(spec, AGENT) == [SESSION, OUTPUTS]


# ── the switch through the ownership merge ───────────────────────────────────


def test_session_to_outputs_detaches_the_session_member_and_keeps_external():
    platform = filesystem_configurations(_spec(outputs=True), AGENT)
    assert platform == [OUTPUTS]
    merged = merge_live([SESSION, EXTERNAL], platform,
                        managed_paths=_published(_spec(outputs=False)))
    assert merged == [OUTPUTS, EXTERNAL]


def test_outputs_to_session_detaches_the_published_outputs_member():
    platform = filesystem_configurations(_spec(outputs=False), AGENT)
    assert platform == [SESSION]
    published = _published(_spec(outputs=True), _spec(outputs=False))
    assert "/mnt/outputs" in published
    assert merge_live([OUTPUTS, EXTERNAL], platform, managed_paths=published) == [
        SESSION, EXTERNAL]
    # without the snapshot history the stale folder would be mistaken for an external mount
    assert merge_live([OUTPUTS, EXTERNAL], platform, managed_paths=set()) == [
        SESSION, OUTPUTS, EXTERNAL]


def test_both_on_keeps_a_live_session_member_and_drops_it_only_when_the_spec_turns_it_off():
    both = _spec(outputs=True, session=True)
    platform = filesystem_configurations(both, AGENT)
    published = _published(_spec(outputs=False), both)
    # a live stale sessionStorage is the platform's own: sent again, not duplicated
    assert merge_live([SESSION, EXTERNAL], platform, managed_paths=published) == [
        SESSION, OUTPUTS, EXTERNAL]
    # the spec turns session storage off: the live member goes, the folder stays
    only_outputs = filesystem_configurations(_spec(outputs=True, session=False), AGENT)
    assert merge_live([SESSION, OUTPUTS, EXTERNAL], only_outputs,
                      managed_paths=published | {"/mnt/workspace"}) == [OUTPUTS, EXTERNAL]


def test_turning_the_output_folder_off_removes_only_the_s3_bucket_mount():
    published = _published(_spec(outputs=True, session=True),
                           _spec(outputs=False, session=True))
    assert "/mnt/outputs" in published
    platform = filesystem_configurations(_spec(outputs=False, session=True), AGENT)
    assert platform == [SESSION]
    assert merge_live([SESSION, OUTPUTS, EXTERNAL], platform, managed_paths=published) == [
        SESSION, EXTERNAL]


class _Live:
    def __init__(self, live):
        self.live = live

    def get_agent_runtime(self, agentRuntimeId):  # noqa: N803
        return {"agentRuntimeId": agentRuntimeId, "filesystemConfigurations": self.live}


@pytest.mark.parametrize("to_outputs", [True, False])
def test_runtime_update_mounts_switches_in_both_directions(to_outputs):
    old, new = (SESSION, OUTPUTS) if to_outputs else (OUTPUTS, SESSION)
    published = _published(_spec(outputs=not to_outputs), _spec(outputs=to_outputs))
    out = runtime_update_mounts(
        _Live([old, EXTERNAL]), "rt-1", _spec(outputs=to_outputs), managed_paths=published,
        vpc=None, log=lambda _line: None, agent_id=AGENT)
    assert out == [new, EXTERNAL]


@pytest.mark.parametrize("to_outputs", [True, False])
def test_harness_environment_switches_in_both_directions(to_outputs):
    old, new = (SESSION, OUTPUTS) if to_outputs else (OUTPUTS, SESSION)
    published = _published(_spec(outputs=not to_outputs), _spec(outputs=to_outputs))
    live = {"environment": {"agentCoreRuntimeEnvironment": {
        "filesystemConfigurations": [old, EXTERNAL]}}}
    env = hc.environment_for_update(live, [new], managed_paths=published)
    assert env["agentCoreRuntimeEnvironment"]["filesystemConfigurations"] == [new, EXTERNAL]


# ── the canary candidate ─────────────────────────────────────────────────────


@pytest.mark.parametrize("to_outputs", [True, False])
def test_a_canary_candidate_honours_the_published_paths(monkeypatch, to_outputs):
    """Production's spec (the agent row) is the OLD choice; the edited spec the new one.
    The old member must not ride along as an "external" mount."""
    old = SESSION if to_outputs else OUTPUTS
    monkeypatch.setattr(canary_infra, "spec_vpc", lambda *_a, **_kw: None)
    db = SessionLocal()
    from app.models.ledger import Agent

    row = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="switch-agent", method="zip_runtime",
                spec=_spec(outputs=not to_outputs).model_dump(mode="json"))
    db.add(row)
    db.commit()
    db.add(SpecSnapshot(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=row.id, seq=1,
                        spec=_spec(outputs=not to_outputs).model_dump(mode="json")))
    db.commit()
    agent_id, row_spec = row.id, row.spec
    db.close()
    agent = type("A", (), {"id": agent_id, "spec": row_spec,
                           "workspace_id": DEFAULT_WORKSPACE_ID})()
    logs: list[str] = []
    merged = canary_infra._candidate_mounts(
        {"filesystemConfigurations": [old, EXTERNAL]}, _spec(outputs=to_outputs), agent,
        logs.append)
    # the platform's own member is the spec's (its folder prefix carries the real agent id)
    assert merged[0] == filesystem_configurations(_spec(outputs=to_outputs), agent_id)[0]
    assert merged[1:] == [EXTERNAL] and old not in merged


def test_a_canary_candidate_with_outputs_maps_the_agent_prefix(monkeypatch):
    monkeypatch.setattr(canary_infra, "spec_vpc", lambda *_a, **_kw: None)
    agent = type("A", (), {"id": AGENT, "spec": {}, "workspace_id": DEFAULT_WORKSPACE_ID})()
    merged = canary_infra._candidate_mounts({"filesystemConfigurations": []},
                                            _spec(outputs=True), agent, lambda _l: None)
    assert merged == [OUTPUTS]


def test_the_prompt_note_header_does_not_call_the_session_folder_shared():
    note = mounts.prompt_note(_spec(outputs=True))
    assert "this session only" in note and "Unless a line says otherwise" in note
    assert json.loads(mounts.mounts_env(_spec(outputs=True))[mounts.ENV_MOUNTS])


def test_the_no_managed_session_storage_sentence_appears_only_when_it_is_off():
    off = mounts.prompt_note(_spec(outputs=True, session=False))
    assert "no managed session storage" in off and "/tmp" in off
    on = mounts.prompt_note(_spec(outputs=True, session=True))
    assert "this session only" in on and "session output folder" in on
    assert "managed session storage" not in on and "/tmp" not in on
