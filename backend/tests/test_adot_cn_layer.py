"""N17-obs465 — the platform's aws-cn trace exporter layer for TypeScript (Node) container agents.

ADOT Node cannot sign its trace export in aws-cn (N17-obs459). The platform adds ONE layer on top
of a Node agent's image in its own build recipe (`app/deployer/adot_cn`): default on in aws-cn for
`container_source` (a package.json depending on ADOT Node) and `container_image` (an image whose
config names Node), opt-out `byoc.cn_trace_exporter: false`, visible on the row (member + layer
image) and in the deploy log. Python containers and every other partition keep today's path byte
for byte. The hook itself is pinned per ADOT Node version by `app/deployer/adot_cn/test`.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.db import SessionLocal
from app.deployer import adot_cn
from app.deployer import byoc as byoc_dep
from app.deployer.image_build import platform_buildspec_path
from app.models.ledger import Agent, ChatSession
from app.schemas.agent import AgentSpec
from app.services import byoc_uploads, trace_status
from tests.conftest import ws_ctx
from tests.test_byoc import (
    RESOURCES,
    SDK_MAIN,
    StubS3,
    _byoc_spec,
    _client_router,
    _get_agent,
    _mk_agent,
    _seed_upload,
    _stage_ctx,
    make_zip,
)

CN = "cn-northwest-1"
DIGEST = "sha256:" + "0" * 64
MEMBER = "sha256:" + "1" * 64
CN_IMAGE = "111122223333.dkr.ecr.cn-northwest-1.amazonaws.com.cn/launchpad-agents:ts-v1"
NODE_PACKAGE = json.dumps({"name": "ts-agent", "dependencies": {
    adot_cn.ADOT_NODE_PACKAGE: "0.13.0", "@strands-agents/sdk": "1.22.0"}}).encode()
NODE_ZIP = {"Dockerfile": b"FROM node:22-slim\nCMD [\"node\", \"main.mjs\"]\n",
            "package.json": NODE_PACKAGE, "main.mjs": b"console.log('hi')\n"}


class StubCodeBuild:
    def __init__(self):
        self.started: list[dict] = []

    def start_build(self, **kwargs):
        self.started.append(kwargs)
        return {"build": {"id": f"b-{len(self.started)}"}}

    def batch_get_builds(self, ids):
        return {"builds": [{"id": ids[0], "currentPhase": "COMPLETED",
                            "buildStatus": "SUCCEEDED", "phases": []}]}


class StubEcr:
    def __init__(self, config: dict | None = None, readable: bool = True):
        self.config, self.readable, self.tags = config, readable, []

    def describe_images(self, repositoryName, imageIds):
        self.tags.append(imageIds[0].get("imageTag"))
        tag = imageIds[0].get("imageTag") or ""
        return {"imageDetails": [{"imageDigest": MEMBER if tag.endswith("-member") else DIGEST}]}

    def batch_get_image(self, repositoryName, imageIds, acceptedMediaTypes):
        if not self.readable:
            raise RuntimeError("AccessDenied")
        return {"images": [{"imageManifest": json.dumps({"config": {"digest": "sha256:cfg"}})}]}

    def get_download_url_for_layer(self, repositoryName, layerDigest):
        return {"downloadUrl": "https://presigned.example/cfg"}


def _env(build: dict) -> dict[str, str]:
    return {e["name"]: e["value"] for e in build["environmentVariablesOverride"]}


def _ctx(monkeypatch, spec: AgentSpec, clients: dict, region: str = CN):
    _client_router(monkeypatch, clients)
    monkeypatch.setattr("app.deployer.image_build.get_settings",
                        lambda: SimpleNamespace(image_scan_enabled=False))
    agent_id, dep_id = _mk_agent(spec)
    ctx = _stage_ctx(agent_id, dep_id, ws_ctx(RESOURCES, region=region))
    logs: list[str] = []
    ctx.log = logs.append  # type: ignore[method-assign]
    return agent_id, ctx, logs


def _shipped(s3: StubS3) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(s3.objects[("bkt", "builds/byoc-agent/source.zip")])) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


# ── container_source ────────────────────────────────────────────────────────────────────


def test_a_node_container_source_in_aws_cn_ships_the_layer_and_records_both_images(
        monkeypatch):
    s3, cb = StubS3(), StubCodeBuild()
    _seed_upload(s3, "u-ts", NODE_ZIP)
    spec = AgentSpec(**_byoc_spec(byoc={"artifact_kind": "container_source", "upload_id": "u-ts"}))
    agent_id, ctx, logs = _ctx(monkeypatch, spec, {"s3": s3, "codebuild": cb, "ecr": StubEcr()})

    result = byoc_dep._stage_package(ctx, _get_agent(agent_id))

    files = _shipped(s3)
    assert files[adot_cn.HOOK_NAME] == adot_cn.HOOK_PATH.read_bytes()
    assert files[adot_cn.LAYER_DOCKERFILE_NAME].decode() == adot_cn.LAYER_DOCKERFILE
    assert files["buildspec.yml"] == platform_buildspec_path().read_bytes()
    assert _env(cb.started[0])["LAUNCHPAD_CN_TRACE_LAYER"] == "1"
    assert "LAUNCHPAD_MEMBER_IMAGE" not in _env(cb.started[0])
    telemetry = _get_agent(agent_id).spec["byoc"]["telemetry"]
    registry = "111122223333.dkr.ecr.cn-northwest-1.amazonaws.com.cn/launchpad-agents"
    assert telemetry["cn_exporter"] == "applied" and telemetry["runtime"] == "node"
    assert telemetry["member_image"] == f"{registry}@{MEMBER}"
    assert telemetry["layer_image"] == f"{registry}@{DIGEST}" == ctx.scratch["image_uri"]
    assert any("aws-cn trace exporter layer" in line and MEMBER in line for line in logs)
    assert "aws-cn trace exporter layer" in result.detail


def test_the_opt_out_keeps_the_members_image(monkeypatch):
    s3, cb = StubS3(), StubCodeBuild()
    _seed_upload(s3, "u-ts", NODE_ZIP)
    spec = AgentSpec(**_byoc_spec(byoc={"artifact_kind": "container_source", "upload_id": "u-ts",
                                        "cn_trace_exporter": False}))
    agent_id, ctx, _logs = _ctx(monkeypatch, spec, {"s3": s3, "codebuild": cb, "ecr": StubEcr()})
    byoc_dep._stage_package(ctx, _get_agent(agent_id))
    assert adot_cn.HOOK_NAME not in _shipped(s3)
    assert "LAUNCHPAD_CN_TRACE_LAYER" not in _env(cb.started[0])
    assert _get_agent(agent_id).spec["byoc"]["telemetry"]["cn_exporter"] == "opted_out"


@pytest.mark.parametrize(("files", "region", "reason"), [
    ({"Dockerfile": b"FROM python:3.13-slim\n", "main.py": SDK_MAIN}, CN, "not_node"),
    (NODE_ZIP, "us-west-2", "not_aws_cn"),
])
def test_python_containers_and_other_partitions_build_exactly_as_before(
        monkeypatch, files, region, reason):
    s3, cb = StubS3(), StubCodeBuild()
    _seed_upload(s3, "u-x", files)
    spec = AgentSpec(**_byoc_spec(byoc={"artifact_kind": "container_source", "upload_id": "u-x"}))
    agent_id, ctx, _logs = _ctx(monkeypatch, spec, {"s3": s3, "codebuild": cb, "ecr": StubEcr()},
                                region=region)
    byoc_dep._stage_package(ctx, _get_agent(agent_id))
    assert set(_shipped(s3)) == {*files, "buildspec.yml"}
    assert set(_env(cb.started[0])) == {"AWS_REGION", "ECR_REGISTRY", "ECR_REPO", "IMAGE_TAG"}
    assert _get_agent(agent_id).spec["byoc"]["telemetry"]["cn_exporter"] == reason


# ── container_image ─────────────────────────────────────────────────────────────────────


def _image_spec(**byoc) -> AgentSpec:
    spec = AgentSpec(**_byoc_spec(byoc={"artifact_kind": "container_image",
                                        "image_uri": CN_IMAGE, **byoc}))
    return spec


def test_a_node_image_in_aws_cn_gets_the_layer_built_from_it_and_the_deploy_uses_it(
        monkeypatch):
    s3, cb = StubS3(), StubCodeBuild()
    monkeypatch.setattr(adot_cn, "_fetch", lambda url: json.dumps(
        {"config": {"Env": ["PATH=/usr/bin", "NODE_VERSION=22.11.0"],
                    "Cmd": ["node", "main.mjs"]}}).encode())
    agent_id, ctx, logs = _ctx(monkeypatch, _image_spec(),
                               {"s3": s3, "codebuild": cb, "ecr": StubEcr()})
    result = byoc_dep._stage_package(ctx, _get_agent(agent_id))
    assert not result.skipped
    assert _env(cb.started[0])["LAUNCHPAD_MEMBER_IMAGE"] == CN_IMAGE
    assert _env(cb.started[0])["LAUNCHPAD_CN_TRACE_LAYER"] == "1"
    assert set(_shipped(s3)) == {"buildspec.yml", adot_cn.HOOK_NAME,
                                 adot_cn.LAYER_DOCKERFILE_NAME}
    row = _get_agent(agent_id)
    telemetry = row.spec["byoc"]["telemetry"]
    assert telemetry["cn_exporter"] == "applied" and telemetry["layer_image"].endswith(DIGEST)
    # the deploy (and a resumed one, whose scratch is gone) runs the layer, not the original
    cfg = byoc_dep._config(AgentSpec(**row.spec))
    assert byoc_dep._container_uri(ctx, row, cfg) == telemetry["layer_image"]
    ctx.scratch.clear()
    assert byoc_dep._container_uri(ctx, row, cfg) == telemetry["layer_image"]
    assert any("FROM" in line and CN_IMAGE.split(":")[0] in line for line in logs)


@pytest.mark.parametrize(("config", "readable", "reason"), [
    ({"config": {"Env": ["PYTHON_VERSION=3.13.0"], "Cmd": ["python", "main.py"]}}, True,
     "not_node"),
    (None, False, "unknown_runtime"),
])
def test_a_python_or_unreadable_image_deploys_unchanged(monkeypatch, config, readable, reason):
    cb = StubCodeBuild()
    monkeypatch.setattr(adot_cn, "_fetch", lambda url: json.dumps(config).encode())
    agent_id, ctx, logs = _ctx(monkeypatch, _image_spec(),
                               {"codebuild": cb, "ecr": StubEcr(config, readable)})
    result = byoc_dep._stage_package(ctx, _get_agent(agent_id))
    assert result.skipped and ctx.scratch["image_uri"] == CN_IMAGE and cb.started == []
    assert _get_agent(agent_id).spec["byoc"]["telemetry"]["cn_exporter"] == reason
    assert (reason == "unknown_runtime") == any("could not read the image config" in line
                                                for line in logs)


def test_an_opted_out_image_is_never_read():
    assert adot_cn.decide("aws-cn", False, None) == (False, "opted_out")


# ── detection, recipe, hook line ────────────────────────────────────────────────────────


def test_the_upload_detector_recognises_adot_node(tmp_path):
    report = byoc_uploads.validate_and_detect(make_zip(tmp_path / "ts.zip", NODE_ZIP))
    assert report["detected"]["telemetry"] == {"adot": "node", "runtime": "node",
                                               "adot_node": "0.13.0"}
    plain = {**NODE_ZIP, "package.json": b'{"dependencies": {"@strands-agents/sdk": "1"}}'}
    report = byoc_uploads.validate_and_detect(make_zip(tmp_path / "plain.zip", plain))
    assert report["detected"]["telemetry"] == {"adot": "absent", "runtime": "node",
                                               "adot_node": None}


@pytest.mark.parametrize(("config", "node"), [
    ({"config": {"Env": ["NODE_VERSION=22.11.0"]}}, True),
    ({"config": {"Entrypoint": ["/usr/local/bin/node"], "Cmd": ["main.cjs"]}}, True),
    ({"config": {"Env": ["PYTHON_VERSION=3.13"], "Cmd": ["python", "app.py"]}}, False),
    ({}, False),
])
def test_an_image_config_names_node(config, node):
    assert adot_cn.image_config_is_node(config) is node


def test_the_layer_keeps_the_members_node_options_and_the_buildspec_knows_it():
    assert 'ENV NODE_OPTIONS="--require /opt/launchpad/launchpad-adot-cn.cjs ${NODE_OPTIONS}"' \
        in adot_cn.LAYER_DOCKERFILE
    assert "FROM ${LAUNCHPAD_MEMBER_IMAGE}" in adot_cn.LAYER_DOCKERFILE
    spec = platform_buildspec_path().read_text()
    assert "LAUNCHPAD_CN_TRACE_LAYER" in spec and adot_cn.LAYER_DOCKERFILE_NAME in spec
    assert "$IMAGE_TAG-member" in spec and "LAUNCHPAD_MEMBER_IMAGE" in spec


@pytest.mark.parametrize(("line", "parsed"), [
    ("launchpad-adot-cn: attached (2 span exporter(s) -> https://xray.cn-northwest-1"
     ".amazonaws.com.cn/v1/traces)", {"state": "attached", "reason": "2 span exporter(s) -> "
                                      "https://xray.cn-northwest-1.amazonaws.com.cn/v1/traces"}),
    ("launchpad-adot-cn: native (ADOT signs the aws-cn endpoint itself)",
     {"state": "native", "reason": "ADOT signs the aws-cn endpoint itself"}),
    ("launchpad-adot-cn: bogus", None),
    ("listening on 8080", None),
])
def test_the_hook_line_parses(line, parsed):
    assert adot_cn.parse_hook_line(line) == parsed


# ── the console badge (GET /api/agents/{id}/trace-status) ───────────────────────────────


class StubLogs:
    def __init__(self, hook: str | None, spans: bool):
        self.hook, self.spans = hook, spans

    def filter_log_events(self, logGroupName, filterPattern, startTime, limit):
        if logGroupName == "aws/spans":
            return {"events": [{"message": "{}"}] if self.spans else []}
        return {"events": [{"timestamp": 1_792_984_000_000, "message": self.hook}]
                if self.hook else []}


def _status_agent(cn_exporter: str = "applied", session: bool = True) -> Agent:
    spec = AgentSpec(**_byoc_spec(byoc={"artifact_kind": "container_source", "upload_id": "u"}))
    agent_id, _dep = _mk_agent(spec)
    db = SessionLocal()
    row = db.get(Agent, agent_id)
    row.resource_id = "eval_ts_strands_a5279d-EXAMPLE014"
    row.spec = {**row.spec, "byoc": {**row.spec["byoc"], "telemetry": {
        "cn_exporter": cn_exporter, "member_image": "m", "layer_image": "l"}}}
    if session:
        db.add(ChatSession(agent_id=agent_id, session_id="s" * 40,
                           last_at=datetime.now(UTC) - timedelta(hours=1)))
    db.commit()
    db.close()
    return _get_agent(agent_id)


@pytest.mark.parametrize(("hook", "spans", "badge"), [
    ("launchpad-adot-cn: attached (2 span exporter(s) -> x)", True, "ok"),
    ("launchpad-adot-cn: attached (2 span exporter(s) -> x)", False, "not_arriving"),
    ("launchpad-adot-cn: unavailable (ADOT internals changed: isAwsOtlpEndpoint)", True,
     "unavailable"),
    ("launchpad-adot-cn: skipped (no ADOT Node in the app)", False, "unavailable"),
])
def test_the_badge_reads_both_signals(hook, spans, badge):
    agent = _status_agent()
    db = SessionLocal()
    try:
        out = trace_status.trace_status(db, agent, ws_ctx(RESOURCES, region=CN),
                                        logs=StubLogs(hook, spans))
    finally:
        db.close()
    assert out["badge"] == badge
    assert out["hook"]["state"] == hook.split()[1]
    assert out["member_image"] == "m" and out["layer_image"] == "l"


def test_the_route_answers_for_a_visible_agent(client, monkeypatch):
    agent = _status_agent(cn_exporter="not_node", session=False)
    monkeypatch.setattr(trace_status, "logs_client", lambda _ws: StubLogs(None, False))
    res = client.get(f"/api/agents/{agent.id}/trace-status")
    assert res.status_code == 200, res.text
    assert res.json()["badge"] == "unknown" and res.json()["cn_exporter"] == "not_node"
    assert client.get("/api/agents/nope/trace-status").status_code == 404


def test_the_trace_status_route_answers_display_zone_times_and_keeps_the_hook_reason(
        client, monkeypatch):
    """obs463 on the new route: `hook.at` is ours (+08:00); the reason text is the hook's."""
    agent = _status_agent()
    reason = "2 span exporter(s) -> https://xray.cn-northwest-1.amazonaws.com.cn/v1/traces"
    monkeypatch.setattr(trace_status, "logs_client",
                        lambda _ws: StubLogs(f"launchpad-adot-cn: attached ({reason})", True))
    body = client.get(f"/api/agents/{agent.id}/trace-status").json()
    assert body["hook"]["at"].endswith("+08:00")
    assert datetime.fromisoformat(body["hook"]["at"]) == datetime.fromtimestamp(
        1_792_984_000, UTC)
    assert body["hook"]["reason"] == reason and body["badge"] == "ok"


class PagedLogs:
    """filter_log_events answers OLDEST first, a page at a time (the live t1 trap: a redeployed
    agent's first page still held the previous image's `attached` line)."""

    def __init__(self, lines: list[tuple[int, str]], page: int = 2):
        self.lines, self.page = sorted(lines), page

    def filter_log_events(self, logGroupName, filterPattern, startTime, limit, nextToken=None):
        if logGroupName == "aws/spans":
            return {"events": []}
        start = int(nextToken or 0)
        chunk = self.lines[start:start + self.page]
        out = {"events": [{"timestamp": t, "message": m} for t, m in chunk]}
        if start + self.page < len(self.lines):
            out["nextToken"] = str(start + self.page)
        return out


def test_the_newest_hook_line_wins_across_pages():
    agent = _status_agent(session=False)
    lines = [(1_792_984_000_000 + i, "launchpad-adot-cn: attached (2 span exporter(s) -> x)")
             for i in range(5)] + [
        (1_793_000_000_000, "launchpad-adot-cn: skipped (traces disabled by the app: "
                            "OTEL_TRACES_EXPORTER=none)")]
    db = SessionLocal()
    try:
        out = trace_status.trace_status(db, agent, ws_ctx(RESOURCES, region=CN),
                                        logs=PagedLogs(lines))
    finally:
        db.close()
    assert out["hook"]["state"] == "skipped" and out["badge"] == "unavailable"
