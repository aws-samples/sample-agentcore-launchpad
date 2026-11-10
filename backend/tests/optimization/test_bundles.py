"""Config bundles (N12 E2-6): immutable local rows + S3 copies, refs, baggage, ARNs."""

import json

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, NotFoundError
from app.optimization import bundles
from app.optimization.models import ConfigBundle
from tests.conftest import ws_ctx
from tests.evaluation.fakes import FakeS3

WS = ws_ctx(resources={"artifacts_bucket": "launchpad-artifacts"}, region="cn-northwest-1")


class _Agent:
    id = "agent-1"


def test_create_bundle_writes_row_and_immutable_s3_copy():
    s3 = FakeS3()
    db = SessionLocal()
    out = bundles.create_bundle(
        WS, _Agent(), name="exp_12345678_control", system_prompt="Be helpful.",
        tool_descriptions={"lookup": "Find orders.", "calc": "Do math."}, db=db, s3=s3,
    )
    assert out["bundle_id"].startswith("bndl_") and out["version"] == 1
    assert out["s3_key"] == f"bundles/agent-1/{out['bundle_id']}/v1.json"
    assert out["arn"] == f"arn:aws-cn:s3:::launchpad-artifacts/{out['s3_key']}"
    assert out["name"] == "exp_12345678_control"
    row = db.get(ConfigBundle, (out["bundle_id"], 1))
    assert row.agent_id == "agent-1" and row.workspace_id == DEFAULT_WORKSPACE_ID
    assert row.components == {"configuration": {
        "system_prompt": "Be helpful.",
        "tools": {"calc": {"description": "Do math."}, "lookup": {"description": "Find orders."}},
    }}
    body = json.loads(s3.objects[("launchpad-artifacts", out["s3_key"])])
    assert body["components"] == row.components and body["version"] == 1
    assert s3.calls[0][1]["ContentType"] == "application/json"
    db.close()


def test_new_version_appends_and_never_overwrites():
    s3 = FakeS3()
    db = SessionLocal()
    first = bundles.create_bundle(WS, _Agent(), name="opt_rec", system_prompt="v1",
                                  tool_descriptions=None, db=db, s3=s3)
    second = bundles.new_version(WS, first["bundle_id"], system_prompt="v2",
                                 tool_descriptions={"t": "d"}, db=db, s3=s3)
    assert second["version"] == 2 and second["bundle_id"] == first["bundle_id"]
    assert second["s3_key"].endswith("/v2.json")
    keys = sorted(k for (_, k) in s3.objects)
    assert keys == [first["s3_key"], second["s3_key"]]
    assert json.loads(s3.objects[("launchpad-artifacts", first["s3_key"])])["components"][
        "configuration"]["system_prompt"] == "v1"  # v1 untouched
    assert bundles.get_bundle(WS, first["bundle_id"], db=db).version == 2
    assert bundles.get_bundle(WS, first["bundle_id"], 1, db=db).components["configuration"][
        "system_prompt"] == "v1"
    with pytest.raises(NotFoundError):
        bundles.new_version(WS, "bndl_missing", system_prompt="x", tool_descriptions=None, db=db)
    with pytest.raises(AppError) as dup:
        bundles._write(WS, "agent-1", first["bundle_id"], 2, "opt_rec", {}, db=db, s3=s3)
    assert dup.value.code == "bundle.version_exists"
    assert [r.version for r in bundles.list_bundles(WS, "agent-1", db=db)] == [1, 2]
    db.close()


def test_refs_and_baggage_encoding():
    assert bundles.bundle_ref("bndl_abc", 3) == "bndl_abc:3"
    assert bundles.parse_ref("bndl_abc:3") == ("bndl_abc", 3)
    with pytest.raises(ValueError):
        bundles.parse_ref("nope")
    baggage = bundles.baggage_for("bndl_abc:3", "exp-1", "T1")
    assert baggage == ("launchpad.bundle=bndl_abc%3A3,"
                       "aws.agentcore.gateway.routing_experiment_arn=exp-1,"
                       "aws.agentcore.gateway.routing_experiment_variant_name=T1")
    # values are percent-encoded so the W3C separators cannot be smuggled in
    assert bundles.baggage_for("a,b;c=d:1", "e", "C").startswith(
        "launchpad.bundle=a%2Cb%3Bc%3Dd%3A1,")
    from app.optimization import routing  # one implementation (W-E3's, test-pinned)

    assert baggage == routing.baggage_for("bndl_abc:3", "exp-1", "T1")
    assert bundles.launchpad_meta("bndl_abc:3", "exp-1", "C") == {
        "bundle": "bndl_abc:3", "experiment": "exp-1", "variant": "C"}
    assert bundles.bundle_key("a", "bndl_x", 2) == "bundles/a/bndl_x/v2.json"


def test_without_an_artifacts_bucket_the_write_is_refused():
    with pytest.raises(AppError) as exc:
        bundles.create_bundle(ws_ctx(), _Agent(), name="n", system_prompt="p",
                              tool_descriptions=None, s3=FakeS3())
    assert exc.value.code == "bundle.workspace_not_bootstrapped"


def test_delete_bundle_objects_removes_rows_and_copies():
    s3 = FakeS3()
    db = SessionLocal()
    one = bundles.create_bundle(WS, _Agent(), name="opt_a", system_prompt="p",
                                tool_descriptions=None, db=db, s3=s3)
    db.close()
    removed = bundles.delete_bundle_objects(
        WS, [bundles.bundle_ref(one["bundle_id"], 1), "garbage", "bndl_x:9"], s3=s3)
    assert removed == 1 and s3.objects == {}
    db = SessionLocal()
    assert db.query(ConfigBundle).count() == 0
    db.close()
