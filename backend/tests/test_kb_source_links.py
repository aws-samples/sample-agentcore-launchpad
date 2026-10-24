"""Cited KB sources open through `GET /api/knowledge-bases/source` — no AWS:

- parse_source_uri: the s3:// and https (virtual-hosted / path-style) forms
- source_link: only objects inside a visible managed KB's data source are signed;
  hidden / foreign / VECTOR KBs and out-of-prefix keys read as not found; a deleted
  object is `kb.source_missing`, an unreadable one `kb.source_unreadable`
- the presigned GET renders text sources inline as text/plain
- the Harness KB prompt asks for titled Markdown links, and the strip still removes
  the legacy closing line of Harnesses deployed before
- the route resolves ahead of `/{kb_id}`
"""

import json

import pytest

from app.core.errors import AppError, NotFoundError
from app.deployer import harness as harness_deployer
from app.schemas.agent import AgentSpec
from app.services import knowledge
from app.services.workspace import WorkspaceContext

from .conftest import client_error, ws_ctx

BUCKET = "launchpad-artifacts-111122223333-us-west-2"
KEY = "kb/KB1/04-qdocs-roles.md"
HTTPS = f"https://{BUCKET}.s3.us-west-2.amazonaws.com/{KEY}"


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        (f"s3://{BUCKET}/{KEY}", (BUCKET, KEY)),
        (HTTPS, (BUCKET, KEY)),
        (f"https://{BUCKET}.s3.amazonaws.com/{KEY}", (BUCKET, KEY)),
        (f"https://{BUCKET}.s3-us-west-2.amazonaws.com/{KEY}", (BUCKET, KEY)),
        (f"https://s3.us-west-2.amazonaws.com/{BUCKET}/{KEY}", (BUCKET, KEY)),
        (f"https://my.dotted.bucket.s3.us-west-2.amazonaws.com/{KEY}", ("my.dotted.bucket", KEY)),
        (
            f"https://{BUCKET}.s3.us-west-2.amazonaws.com/kb/KB1/%E5%91%A8%E6%8A%A5.md",
            (BUCKET, "kb/KB1/周报.md"),
        ),
    ],
)
def test_parse_source_uri_accepts_s3_object_addresses(uri, expected):
    assert knowledge.parse_source_uri(uri) == expected


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "s3://bucket-only",
        f"http://{BUCKET}.s3.us-west-2.amazonaws.com/{KEY}",  # not https
        "https://example.com/kb/doc.md",
        f"https://{BUCKET}.s3.us-west-2.amazonaws.com/",  # no key
        "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/x",
        "javascript:alert(1)",
    ],
)
def test_parse_source_uri_rejects_everything_else(uri):
    assert knowledge.parse_source_uri(uri) is None


class StubAgent:
    """bedrock-agent: KB1 (MANAGED, source BUCKET/kb/KB1/), KB2 (VECTOR, same bucket)."""

    def __init__(self):
        self.kbs = {
            "KB1": {"name": "it-helpdesk", "type": "MANAGED", "prefix": "kb/KB1/"},
            "KB2": {"name": "legacy", "type": "VECTOR", "prefix": ""},
        }

    def list_knowledge_bases(self, **_kw):
        return {"knowledgeBaseSummaries": [{"knowledgeBaseId": k} for k in self.kbs]}

    def get_knowledge_base(self, knowledgeBaseId):
        kb = self.kbs[knowledgeBaseId]
        return {
            "knowledgeBase": {
                "name": kb["name"],
                "knowledgeBaseConfiguration": {"type": kb["type"]},
            }
        }

    def list_data_sources(self, knowledgeBaseId, **_kw):
        return {"dataSourceSummaries": [{"dataSourceId": f"DS-{knowledgeBaseId}"}]}

    def get_data_source(self, knowledgeBaseId, dataSourceId):
        params = {
            "connectionConfiguration": {"bucketName": BUCKET},
            "filterConfiguration": {"inclusionPrefixes": [self.kbs[knowledgeBaseId]["prefix"]]},
        }
        conf = {"managedKnowledgeBaseConnectorConfiguration": {
            "connectorParameters": json.dumps(params)}}
        return {"dataSource": {"dataSourceConfiguration": conf}}


class StubS3:
    def __init__(self, head_error=None):
        self.head_error = head_error
        self.presigned = None

    def head_object(self, Bucket, Key):
        if self.head_error:
            raise self.head_error
        return {}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presigned = (op, Params, ExpiresIn)
        return f"https://signed.example/{Params['Key']}"


@pytest.fixture
def stubs(monkeypatch):
    agent, s3 = StubAgent(), StubS3()
    monkeypatch.setattr(knowledge, "agent_client", lambda _ws: agent)
    monkeypatch.setattr(WorkspaceContext, "client", lambda self, service, **kw: s3)
    return agent, s3


def test_source_link_signs_a_visible_managed_kb_document(stubs):
    _, s3 = stubs
    out = knowledge.source_link(ws_ctx(), HTTPS, lambda _kb: True)
    assert out["kb_id"] == "KB1" and out["kb_name"] == "it-helpdesk"
    assert out["name"] == "04-qdocs-roles.md"
    assert out["url"] == f"https://signed.example/{KEY}"
    op, params, ttl = s3.presigned
    assert op == "get_object" and ttl == knowledge.SOURCE_URL_TTL_S
    assert params["Bucket"] == BUCKET and params["Key"] == KEY
    assert params["ResponseContentType"] == "text/plain; charset=utf-8"
    assert params["ResponseContentDisposition"].startswith("inline;")


def test_source_link_keeps_the_stored_type_of_a_binary_document(stubs):
    _, s3 = stubs
    knowledge.source_link(ws_ctx(), f"s3://{BUCKET}/kb/KB1/handbook.pdf", lambda _kb: True)
    assert "ResponseContentType" not in s3.presigned[1]


def test_source_link_hides_a_kb_the_viewer_cannot_see(stubs):
    with pytest.raises(NotFoundError) as err:
        knowledge.source_link(ws_ctx(), HTTPS, lambda kb: kb != "KB1")
    assert err.value.code == "kb.source_not_found"


@pytest.mark.parametrize(
    "uri",
    [
        f"s3://{BUCKET}/kb/OTHER/doc.md",  # same bucket, outside every managed prefix
        f"s3://another-bucket/{KEY}",
    ],
)
def test_source_link_never_signs_outside_a_managed_data_source(stubs, uri):
    _, s3 = stubs
    with pytest.raises(NotFoundError):
        knowledge.source_link(ws_ctx(), uri, lambda _kb: True)
    assert s3.presigned is None


def test_source_link_rejects_a_non_s3_address(stubs):
    with pytest.raises(AppError) as err:
        knowledge.source_link(ws_ctx(), "https://example.com/x.md", lambda _kb: True)
    assert err.value.code == "kb.source_unsupported" and err.value.status_code == 400


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (client_error("404", "Not Found", "HeadObject"), "kb.source_missing"),
        (client_error("403", "Forbidden", "HeadObject"), "kb.source_unreadable"),
    ],
)
def test_source_link_reports_a_deleted_or_unreadable_object(stubs, error, code):
    _, s3 = stubs
    s3.head_error = error
    with pytest.raises(AppError) as err:
        knowledge.source_link(ws_ctx(), HTTPS, lambda _kb: True)
    assert err.value.code == code
    assert s3.presigned is None


def _kb_spec(prompt: str = "help") -> AgentSpec:
    return AgentSpec(
        name="support-bot", method="harness", system_prompt=prompt,
        knowledge_bases=[{"kb_id": "KB1", "name": "it-helpdesk", "description": "docs"}],
    )


def test_harness_kb_prompt_asks_for_titled_markdown_links():
    section = harness_deployer._kb_prompt(_kb_spec())
    assert section.endswith(harness_deployer._KB_END)
    assert "Markdown link" in section and "never paste a bare URI" in section


def test_strip_removes_the_section_a_harness_was_deployed_with_before():
    spec = _kb_spec()
    legacy = harness_deployer._kb_prompt(spec).replace(
        harness_deployer._KB_END, harness_deployer._KB_END_LEGACY
    )
    # a recommendation reflowed the bullets, so only marker → closing line can match
    reflowed = "help" + legacy.replace("Mounted knowledge bases:", "Mounted KBs:")
    assert harness_deployer.strip_generated_prompt(spec, reflowed) == "help"
    current = "help" + harness_deployer._kb_prompt(spec).replace("Mounted knowledge", "KBs")
    assert harness_deployer.strip_generated_prompt(spec, current) == "help"


def test_source_route_resolves_ahead_of_the_kb_detail_route():
    from app.routers.knowledge import router

    gets = [r.path for r in router.routes if "GET" in getattr(r, "methods", ())]
    assert gets.index("/api/knowledge-bases/source") < gets.index("/api/knowledge-bases/{kb_id}")
