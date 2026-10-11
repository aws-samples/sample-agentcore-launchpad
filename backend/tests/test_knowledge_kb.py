"""Managed KB unit coverage — pure logic, no AWS:

- AgentSpec.knowledge_bases validation (method gating, KnowledgeBaseRef shape)
- kb_gateway target-name sanitization
- knowledge external-source validation (bucket/prefix → IAM policy safety)
- filename safety, per-KB inline policy scoping
- harness build_create_params KB prompt + gateway attach
- wrap_params_for_update empty tools/skills detach semantics
- kb_gateway.sync_agentic_target create/delete + ensure_retrieve_target race
"""


import logging
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.core.errors import AppError
from app.deployer.harness import build_create_params
from app.schemas.agent import AgentSpec, KnowledgeBaseRef
from app.services import kb_gateway, knowledge
from app.services.agentcore import harness as hc

from .conftest import client_error, ws_ctx

ROLE_ARN = "arn:aws:iam::111:role/launchpad-agent-execution-role"
KB_GW = {"arn": "arn:aws:...:gateway/launchpad-kb-gw", "oauth_provider_arn": "arn:aws:...:oauth/p"}


def _spec(**over):
    base = {"name": "support-bot", "method": "harness", "system_prompt": "You help users."}
    base.update(over)
    return AgentSpec(**base)


# ── AgentSpec.knowledge_bases validation ─────────────────────────────────────


@pytest.mark.parametrize("method", ["harness", "zip_runtime", "container"])
def test_kb_allowed_on_every_code_and_harness_method(method):
    spec = _spec(method=method, knowledge_bases=[{"kb_id": "ABC123", "name": "docs"}])
    assert [kb.kb_id for kb in spec.knowledge_bases] == ["ABC123"]


def test_kb_rejected_on_studio_canvas():
    with pytest.raises(ValidationError, match="Strands Studio canvas"):
        _spec(
            method="studio",
            code="print('x')",
            knowledge_bases=[{"kb_id": "ABC123", "name": "docs"}],
        )


def test_kb_rejected_on_a2a_protocol():
    with pytest.raises(ValidationError, match="protocol=a2a"):
        _spec(
            method="zip_runtime",
            protocol="a2a",
            knowledge_bases=[{"kb_id": "ABC123", "name": "docs"}],
        )


def test_kb_on_harness_ok():
    spec = _spec(
        knowledge_bases=[{"kb_id": "ABC123XYZ", "name": "Product Docs", "description": "d"}]
    )
    assert spec.knowledge_bases[0].kb_id == "ABC123XYZ"


def test_kb_default_empty():
    assert _spec().knowledge_bases == []


@pytest.mark.parametrize("bad_id", ["", "abc-123", "kb/xyz", "a" * 33, "has space"])
def test_kb_id_pattern_rejected(bad_id):
    with pytest.raises(ValidationError):
        KnowledgeBaseRef(kb_id=bad_id)


def test_kb_list_capped_at_ten():
    refs = [{"kb_id": f"KB{i:08d}"} for i in range(11)]
    with pytest.raises(ValidationError):
        _spec(knowledge_bases=refs)


# ── kb_gateway target-name sanitization ──────────────────────────────────────


def test_retrieve_target_name_slug_and_suffix():
    name = kb_gateway.retrieve_target_name("K5YAKY", "Product Docs & FAQs!")
    assert name == "product-docs-faqs-k5yaky"


def test_retrieve_target_name_blank_name_falls_back():
    assert kb_gateway.retrieve_target_name("ABC12", "  ").startswith("kb-")


def test_agentic_target_name():
    assert kb_gateway.agentic_target_name("Support Bot") == "agentic-support-bot"
    assert kb_gateway.agentic_target_name("") == "agentic-agent"


def test_sanitize_truncates_and_trims():
    assert kb_gateway._sanitize("A" * 100, 10) == "a" * 10
    assert kb_gateway._sanitize("--weird__name--", 30) == "weird-name"


# ── external-source validation (blocks IAM policy widening) ───────────────────


def test_resolve_source_existing_ok():
    assert knowledge._resolve_source(
        ws_ctx(), "KB1", {"mode": "existing", "bucket": "my-corpus"}
    ) == ("my-corpus", "")
    assert knowledge._resolve_source(
        ws_ctx(), "KB1", {"mode": "existing", "bucket": "my-corpus", "prefix": "/docs/"}
    ) == ("my-corpus", "docs/")


@pytest.mark.parametrize("bucket", ["*", "My-Bucket", "bad/bucket", "ab", "has space", "x" * 64])
def test_resolve_source_rejects_bad_bucket(bucket):
    with pytest.raises(AppError) as ei:
        knowledge._resolve_source(ws_ctx(), "KB1", {"mode": "existing", "bucket": bucket})
    assert ei.value.code == "kb.invalid_bucket"
    assert ei.value.status_code == 400


def test_resolve_source_rejects_wildcard_prefix():
    with pytest.raises(AppError) as ei:
        knowledge._resolve_source(
            ws_ctx(), "KB1", {"mode": "existing", "bucket": "corpus", "prefix": "a*"}
        )
    assert ei.value.code == "kb.invalid_prefix"


def test_resolve_source_requires_bucket():
    with pytest.raises(AppError) as ei:
        knowledge._resolve_source(ws_ctx(), "KB1", {"mode": "existing", "bucket": "  "})
    assert ei.value.code == "kb.bucket_required"


def test_resolve_source_bad_mode():
    with pytest.raises(AppError) as ei:
        knowledge._resolve_source(ws_ctx(), "KB1", {"mode": "ftp"})
    assert ei.value.code == "kb.invalid_source"


# ── filename safety + per-KB inline policy scoping ───────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../etc/passwd", "passwd"),
        ("a/b/c.txt", "c.txt"),
        ("C:\\Users\\x\\f.txt", "f.txt"),
        ("", "file"),
        ("..", "file"),
        (".", "file"),
        ("report.pdf", "report.pdf"),
    ],
)
def test_safe_filename(raw, expected):
    assert knowledge._safe_filename(raw) == expected


def test_kb_policy_document_scopes_to_prefix():
    doc = knowledge._kb_policy_document("corp-docs", "docs/")
    get_stmt, list_stmt = doc["Statement"]
    assert get_stmt["Action"] == ["s3:GetObject"]
    assert get_stmt["Resource"] == "arn:aws:s3:::corp-docs/docs/*"
    assert list_stmt["Resource"] == "arn:aws:s3:::corp-docs"
    assert list_stmt["Condition"] == {"StringLike": {"s3:prefix": ["docs/*"]}}


def test_kb_policy_document_no_prefix_has_no_list_condition():
    doc = knowledge._kb_policy_document("corp-docs", "")
    get_stmt, list_stmt = doc["Statement"]
    assert get_stmt["Resource"] == "arn:aws:s3:::corp-docs/*"
    assert "Condition" not in list_stmt


def test_kb_role_name_uses_bootstrapped_role_arn():
    workspace = ws_ctx(
        {"kb_role_arn": "arn:aws:iam::111:role/launchpad-kb-role-us-east-1"}
    )
    assert knowledge._kb_role_name(workspace) == "launchpad-kb-role-us-east-1"


def test_kb_role_name_falls_back_before_bootstrap():
    assert knowledge._kb_role_name(ws_ctx()) == "launchpad-kb-role"


# ── harness build_create_params: KB prompt + gateway attach ──────────────────


def test_build_params_no_kb_leaves_prompt_and_tools_clean():
    params = build_create_params(_spec(), ROLE_ARN, None, kb_gateway=KB_GW)
    assert params["systemPrompt"] == [{"text": "You help users."}]
    assert "tools" not in params  # no KB selected → no kb-gw attach


def test_build_params_kb_injects_prompt_and_attaches_gateway():
    spec = _spec(
        knowledge_bases=[
            {"kb_id": "KB1", "name": "Product Docs", "description": "how features work"}
        ]
    )
    params = build_create_params(spec, ROLE_ARN, None, kb_gateway=KB_GW)
    prompt = params["systemPrompt"][0]["text"]
    assert "Knowledge bases" in prompt
    assert "agentic-support-bot___AgenticRetrieveStream" in prompt
    assert "product-docs-kb1___Retrieve" in prompt
    assert "how features work" in prompt
    gw_tools = [t for t in params["tools"] if t["name"] == "launchpad_kb_gw"]
    assert len(gw_tools) == 1
    assert gw_tools[0]["config"]["agentCoreGateway"]["gatewayArn"] == KB_GW["arn"]


def test_build_params_kb_without_gateway_config_skips_attach():
    """Half-bootstrapped env (no kb-gw resources): prompt still lists the KBs but
    no gateway tool is attached rather than emitting a malformed tool."""
    spec = _spec(knowledge_bases=[{"kb_id": "KB1", "name": "Docs"}])
    params = build_create_params(spec, ROLE_ARN, None, kb_gateway=None)
    assert "Knowledge bases" in params["systemPrompt"][0]["text"]
    assert "tools" not in params


# ── wrap_params_for_update: empty tools/skills detach ────────────────────────


def test_wrap_update_sends_empty_tools_when_last_removed():
    """A spec with no tools/skills must send explicit [] so UpdateHarness detaches
    the previously-mounted tools (e.g. the only KB) instead of keeping them."""
    update = hc.wrap_params_for_update(build_create_params(_spec(), ROLE_ARN, None))
    assert update["tools"] == []
    assert update["skills"] == []


def test_wrap_update_preserves_present_tools():
    spec = _spec(tools=[{"type": "builtin", "name": "browser"}])
    params = build_create_params(spec, ROLE_ARN, None)
    update = hc.wrap_params_for_update(params)
    assert update["tools"] == [{"type": "agentcore_browser", "name": "browser"}]


# ── kb_gateway target sync with a stubbed control plane ──────────────────────


class _Conflict(Exception):
    pass


class StubControl:
    """Minimal bedrock-agentcore-control stub: an in-memory target map."""

    def __init__(self):
        self.targets: dict[str, dict] = {}
        self.creates = 0
        self.updates = 0
        self.deletes = 0

        class _Exc:
            ConflictException = _Conflict

        self.exceptions = _Exc()

    def list_gateway_targets(self, gatewayIdentifier, maxResults=100):
        return {"items": list(self.targets.values())}

    def create_gateway_target(self, **kw):
        self.creates += 1
        name = kw["name"]
        if name in self.targets:
            raise self.exceptions.ConflictException()
        tid = f"tid-{name}"
        self.targets[name] = {"name": name, "targetId": tid, "status": "READY"}
        return {"targetId": tid}

    def update_gateway_target(self, **kw):
        self.updates += 1
        return {"targetId": kw["targetId"]}

    def delete_gateway_target(self, gatewayIdentifier, targetId):
        self.deletes += 1
        for n, t in list(self.targets.items()):
            if t["targetId"] == targetId:
                del self.targets[n]

    def get_gateway_target(self, gatewayIdentifier, targetId):
        return {"status": "READY"}


def test_sync_agentic_target_noop_when_empty_and_absent():
    ctl = StubControl()
    assert kb_gateway.sync_agentic_target(ctl, "gw", "bot", []) is None
    assert ctl.creates == 0 and ctl.deletes == 0


def test_sync_agentic_target_deletes_when_emptied():
    ctl = StubControl()
    tid = kb_gateway.sync_agentic_target(ctl, "gw", "bot", [{"kb_id": "KB1", "description": "d"}])
    assert tid == "tid-agentic-bot"
    assert kb_gateway.sync_agentic_target(ctl, "gw", "bot", []) is None
    assert ctl.deletes == 1
    assert kb_gateway.agentic_target_name("bot") not in ctl.targets


def test_sync_agentic_target_updates_existing():
    ctl = StubControl()
    kb_gateway.sync_agentic_target(ctl, "gw", "bot", [{"kb_id": "KB1", "description": "d"}])
    kb_gateway.sync_agentic_target(ctl, "gw", "bot", [{"kb_id": "KB2", "description": "e"}])
    assert ctl.updates == 1  # second call updates rather than recreates


class StubRace(StubControl):
    """create_gateway_target loses a race: raises Conflict but the target then
    appears (as if a concurrent publish created it)."""

    def __init__(self, name):
        super().__init__()
        self._name = name
        self._revealed = False

    def list_gateway_targets(self, gatewayIdentifier, maxResults=100):
        if self._revealed:
            return {"items": [{"name": self._name, "targetId": "tid-race", "status": "READY"}]}
        return {"items": []}

    def create_gateway_target(self, **kw):
        self.creates += 1
        self._revealed = True
        raise self.exceptions.ConflictException()


def test_ensure_retrieve_target_adopts_racing_winner():
    tname = kb_gateway.retrieve_target_name("KB1", "Docs")
    ctl = StubRace(tname)
    tid = kb_gateway.ensure_retrieve_target(ctl, "gw", "KB1", "Docs", "d")
    assert tid == "tid-race"
    assert ctl.creates == 1  # we tried once, then adopted the winner


def test_ensure_retrieve_target_creates_when_absent():
    ctl = StubControl()
    tid = kb_gateway.ensure_retrieve_target(ctl, "gw", "KB1", "Docs", "d")
    assert tid == f"tid-{kb_gateway.retrieve_target_name('KB1', 'Docs')}"
    # idempotent: second call finds the existing target, no second create
    tid2 = kb_gateway.ensure_retrieve_target(ctl, "gw", "KB1", "Docs", "d")
    assert tid2 == tid and ctl.creates == 1


# ── force-delete strips the KB from mounted agents ──────────────────────────


def test_strip_kb_from_agents_updates_specs(monkeypatch):
    from app.core.db import SessionLocal
    from app.models.ledger import Agent
    from app.services import knowledge

    db = SessionLocal()
    mounted = Agent(
        workspace_id="default",
        name="kb-user", method="harness", status="active",
        spec={"name": "kb-user", "method": "harness",
              "knowledge_bases": [
                  {"kb_id": "KBDEAD0001", "name": "dead", "description": "d"},
                  {"kb_id": "KBLIVE0002", "name": "live", "description": "l"},
              ]},
    )
    other = Agent(
        workspace_id="default",
        name="kb-free", method="harness", status="active",
        spec={"name": "kb-free", "method": "harness"},
    )
    # zip/container agents retrieve directly — they must never touch kb-gw
    direct = Agent(
        workspace_id="default",
        name="kb-direct", method="zip_runtime", status="active",
        spec={"name": "kb-direct", "method": "zip_runtime",
              "knowledge_bases": [
                  {"kb_id": "KBDEAD0001", "name": "dead", "description": "d"},
                  {"kb_id": "KBLIVE0002", "name": "live", "description": "l"},
              ]},
    )
    db.add_all([mounted, other, direct])
    db.commit()
    db.close()

    synced: list[tuple[str, list[str]]] = []

    workspace = ws_ctx({"kb_gateway_id": "gw-1"}, id="default")
    monkeypatch.setattr(knowledge, "control_client", lambda _ws: object())
    monkeypatch.setattr(
        knowledge.kb_gateway, "sync_agentic_target",
        lambda control, gw, name, refs: synced.append((name, [r["kb_id"] for r in refs])),
    )

    stripped = knowledge._strip_kb_from_agents(workspace, "KBDEAD0001")

    assert stripped == ["kb-user", "kb-direct"]
    # only the harness agent's gateway target is resynced
    assert synced == [("kb-user", ["KBLIVE0002"])]
    db = SessionLocal()
    for name in ("kb-user", "kb-direct"):
        row = db.query(Agent).filter(Agent.name == name).one()
        assert [r["kb_id"] for r in row.spec["knowledge_bases"]] == ["KBLIVE0002"]
    db.close()


def _mount_one_harness_agent(name: str) -> None:
    from app.core.db import SessionLocal
    from app.models.ledger import Agent

    db = SessionLocal()
    db.add(Agent(
        workspace_id="default", name=name, method="harness", status="active",
        spec={"name": name, "method": "harness",
              "knowledge_bases": [{"kb_id": "KBDEAD0001", "name": "dead"}]},
    ))
    db.commit()
    db.close()


def _strip_with_failing_resync(monkeypatch, exc: Exception) -> list[str]:
    def fail(control, gw, name, refs):
        raise exc

    monkeypatch.setattr(knowledge, "control_client", lambda _ws: object())
    monkeypatch.setattr(knowledge.kb_gateway, "sync_agentic_target", fail)
    return knowledge._strip_kb_from_agents(
        ws_ctx({"kb_gateway_id": "gw-1"}, id="default"), "KBDEAD0001"
    )


def test_strip_kb_resync_on_a_gone_gateway_is_quiet(monkeypatch, caplog):
    _mount_one_harness_agent("kb-gone")
    caplog.set_level(logging.WARNING, logger=knowledge.__name__)
    stripped = _strip_with_failing_resync(
        monkeypatch, client_error("ResourceNotFoundException")
    )
    assert stripped == ["kb-gone"]
    assert caplog.records == []


def test_strip_kb_resync_failure_warns_and_still_strips(monkeypatch, caplog):
    from app.core.db import SessionLocal
    from app.models.ledger import Agent

    _mount_one_harness_agent("kb-denied")
    caplog.set_level(logging.WARNING, logger=knowledge.__name__)
    stripped = _strip_with_failing_resync(monkeypatch, client_error("AccessDeniedException"))
    assert stripped == ["kb-denied"]
    (record,) = caplog.records
    text = record.getMessage()
    assert "KBDEAD0001" in text and "kb-denied" in text and "gw-1" in text
    assert "AccessDeniedException" in text
    db = SessionLocal()
    row = db.query(Agent).filter(Agent.name == "kb-denied").one()
    assert row.spec["knowledge_bases"] == []  # the ledger strip is not rolled back
    db.close()


# ── force/plain delete: best-effort cleanups ─────────────────────────────────


@pytest.mark.parametrize(
    ("error", "warned"),
    [(None, False), (client_error("NoSuchEntity"), False),
     (client_error("AccessDenied"), True)],
)
def test_delete_kb_policy_warns_only_when_the_policy_is_left(
    monkeypatch, caplog, error, warned
):
    from app.services import aws_clients

    iam = MagicMock()
    iam.delete_role_policy.side_effect = error
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: iam)
    caplog.set_level(logging.WARNING, logger=knowledge.__name__)
    workspace = ws_ctx({"kb_role_arn": "arn:aws:iam::1:role/kb-role"})
    knowledge._delete_kb_policy(workspace, "KB1")  # never raises
    iam.delete_role_policy.assert_called_once_with(
        RoleName="kb-role", PolicyName="launchpad-kb-KB1"
    )
    assert len(caplog.records) == int(warned)
    if warned:
        text = caplog.records[0].getMessage()
        assert "launchpad-kb-KB1" in text and "kb-role" in text and "AccessDenied" in text


def test_delete_kb_warns_only_for_data_sources_not_already_gone(monkeypatch, caplog):
    client = MagicMock()
    client.get_knowledge_base.return_value = {
        "knowledgeBase": {"knowledgeBaseConfiguration": {"type": "MANAGED"}}
    }
    client.delete_data_source.side_effect = [
        None,
        client_error("ResourceNotFoundException"),
        client_error("ThrottlingException"),
    ]
    monkeypatch.setattr(knowledge, "agent_client", lambda _ws: client)
    monkeypatch.setattr(
        knowledge, "_list_data_sources",
        lambda c, kb: [{"dataSourceId": "DS-OK"}, {"dataSourceId": "DS-GONE"},
                       {"dataSourceId": "DS-SLOW"}],
    )
    monkeypatch.setattr(knowledge, "_delete_kb_policy", lambda ws, kb: None)
    caplog.set_level(logging.WARNING, logger=knowledge.__name__)

    out = knowledge.delete_kb(ws_ctx({}, id="default"), "KB1")

    assert out == {"deleted": True, "kb_id": "KB1"}
    client.delete_knowledge_base.assert_called_once_with(knowledgeBaseId="KB1")
    (record,) = caplog.records
    text = record.getMessage()
    assert "KB1" in text and "DS-SLOW" in text and "ThrottlingException" in text


class FailingDeleteControl(StubControl):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def delete_gateway_target(self, gatewayIdentifier, targetId):
        self.deletes += 1
        raise self.exc


@pytest.mark.parametrize(
    ("error", "warned"),
    [(client_error("ResourceNotFoundException"), False),
     (client_error("AccessDeniedException"), True)],
)
def test_delete_target_warns_only_when_the_target_is_left(caplog, error, warned):
    ctl = FailingDeleteControl(error)
    caplog.set_level(logging.WARNING, logger=kb_gateway.__name__)
    kb_gateway._delete_target(ctl, "gw-1", "tid-1")  # never raises
    assert ctl.deletes == 1
    assert len(caplog.records) == int(warned)
    if warned:
        text = caplog.records[0].getMessage()
        assert "gw-1" in text and "tid-1" in text and "AccessDeniedException" in text


def test_delete_target_success_is_quiet(caplog):
    ctl = StubControl()
    ctl.targets["t"] = {"name": "t", "targetId": "tid-1", "status": "READY"}
    caplog.set_level(logging.WARNING, logger=kb_gateway.__name__)
    kb_gateway._delete_target(ctl, "gw-1", "tid-1")
    assert ctl.targets == {} and caplog.records == []


# ── data-source document listing ─────────────────────────────────────────────


def _docs_client(next_token=None):
    from unittest.mock import MagicMock

    client = MagicMock()
    client.get_knowledge_base.return_value = {
        "knowledgeBase": {"knowledgeBaseConfiguration": {"type": "MANAGED"}}
    }
    client.list_knowledge_base_documents.return_value = {
        "documentDetails": [
            {
                "status": "INDEXED",
                "identifier": {"s3": {"uri": "s3://bkt/kb/KB1/guide.pdf"}},
                "updatedAt": "2026-07-13T06:12:42+00:00",
            },
            {
                "status": "FAILED",
                "statusReason": "unsupported format",
                "identifier": {"s3": {"uri": "s3://bkt/kb/KB1/weird.bin"}},
            },
        ],
        **({"nextToken": next_token} if next_token else {}),
    }
    # connectorParameters as a JSON STRING — the GetDataSource document quirk
    client.get_data_source.return_value = {
        "dataSource": {
            "dataSourceConfiguration": {
                "managedKnowledgeBaseConnectorConfiguration": {
                    "connectorParameters": (
                        '{"connectionConfiguration": {"bucketName": "bkt"},'
                        ' "filterConfiguration": {"inclusionPrefixes": ["kb/KB1/"]}}'
                    )
                }
            }
        }
    }
    return client


def test_list_documents_joins_s3_meta_and_paginates(monkeypatch):
    client = _docs_client(next_token="tok-2")
    monkeypatch.setattr(knowledge, "agent_client", lambda _ws=None: client)
    monkeypatch.setattr(
        knowledge,
        "_s3_object_meta",
        lambda _ws, bucket, prefix: {
            "kb/KB1/guide.pdf": (361727, "2026-07-13T06:09:31+00:00")
        },
    )
    out = knowledge.list_documents(ws_ctx(), "KB1", "DS1", page_size=2, token="tok-1")

    kwargs = client.list_knowledge_base_documents.call_args.kwargs
    assert kwargs == {
        "knowledgeBaseId": "KB1", "dataSourceId": "DS1",
        "maxResults": 2, "nextToken": "tok-1",
    }
    assert out["next_token"] == "tok-2"
    first, second = out["documents"]
    assert first == {
        "name": "guide.pdf",
        "uri": "s3://bkt/kb/KB1/guide.pdf",
        "status": "INDEXED",
        "status_reason": None,
        "indexed_at": "2026-07-13T06:12:42+00:00",
        "size_bytes": 361727,
        "uploaded_at": "2026-07-13T06:09:31+00:00",
    }
    # not in the S3 map + FAILED reason surfaced
    assert second["size_bytes"] is None and second["uploaded_at"] is None
    assert second["status_reason"] == "unsupported format"


def test_list_documents_degrades_without_s3_access(monkeypatch):
    client = _docs_client()
    monkeypatch.setattr(knowledge, "agent_client", lambda _ws=None: client)

    def denied(_ws, bucket, prefix):
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(knowledge, "_s3_object_meta", denied)
    out = knowledge.list_documents(ws_ctx(), "KB1", "DS1")
    assert out["next_token"] is None
    assert all(d["size_bytes"] is None for d in out["documents"])
    assert [d["name"] for d in out["documents"]] == ["guide.pdf", "weird.bin"]


# ── Playground query — KB-side retrieval failures surface as 502 ────────────


class _BrokenRuntime:
    class exceptions:
        class ValidationException(Exception):
            pass

        class AccessDeniedException(Exception):
            pass

    def retrieve(self, **kw):
        raise self.exceptions.AccessDeniedException("[security_exception] 403")


def test_query_kb_side_failure_maps_to_502(monkeypatch):
    monkeypatch.setattr(
        knowledge, "agent_runtime_client", lambda _ws=None: _BrokenRuntime()
    )
    with pytest.raises(AppError) as exc_info:
        knowledge.query(ws_ctx(), "KB1", "hello")
    assert exc_info.value.status_code == 502
    assert exc_info.value.code == "kb.query_failed"


# ── slow-path data-source completion (backend-owned, idempotent) ─────────────


class _KBClientStub:
    """Minimal bedrock-agent stub: a KB that turns ACTIVE after N polls."""

    def __init__(self, active_after: int = 2, sources: list[dict] | None = None):
        self._polls = 0
        self._active_after = active_after
        self._sources = sources or []
        self.created: list[dict] = []

    def get_knowledge_base(self, knowledgeBaseId: str):  # noqa: N803 (AWS shape)
        self._polls += 1
        status = "ACTIVE" if self._polls >= self._active_after else "CREATING"
        return {
            "knowledgeBase": {
                "knowledgeBaseId": knowledgeBaseId,
                "name": "docs",
                "status": status,
                "knowledgeBaseConfiguration": {"type": "MANAGED"},
            }
        }

    def list_data_sources(self, **_kw):
        return {"dataSourceSummaries": self._sources}

    def get_data_source(self, knowledgeBaseId: str, dataSourceId: str):  # noqa: N803
        detail = next(s["detail"] for s in self._sources if s["dataSourceId"] == dataSourceId)
        return {"dataSource": detail}

    def create_data_source(self, **kw):
        self.created.append(kw)
        return {"dataSource": {"dataSourceId": f"ds-{len(self.created)}"}}


def _ds_detail(bucket: str, prefix: str) -> dict:
    return {
        "dataSourceConfiguration": {
            "type": "MANAGED_KNOWLEDGE_BASE_CONNECTOR",
            "managedKnowledgeBaseConnectorConfiguration": {
                "connectorParameters": {
                    "type": "S3",
                    "connectionConfiguration": {"bucketName": bucket},
                    "filterConfiguration": {"inclusionPrefixes": [prefix]},
                }
            },
        }
    }


def test_source_completion_creates_source_once_kb_is_active(monkeypatch):
    """The browser used to own this replay; losing the page lost the source."""
    stub = _KBClientStub(active_after=2)
    monkeypatch.setattr(knowledge, "agent_client", lambda _ws=None: stub)
    monkeypatch.setattr(knowledge.time, "sleep", lambda _s: None)

    thread = knowledge._start_source_completion(
        ws_ctx({"artifacts_bucket": "artifacts-bkt"}),
        "KB123",
        {"mode": "upload"},
        interval_s=0,
    )
    thread.join(timeout=5)

    assert len(stub.created) == 1
    assert stub.created[0]["knowledgeBaseId"] == "KB123"


def test_source_completion_is_idempotent(monkeypatch):
    """Fast path, background thread and a manual POST can all race — the guard
    must return the existing source instead of adding a second connector."""
    existing = [{"dataSourceId": "ds-old", "detail": _ds_detail("artifacts-bkt", "kb/KB123/")}]
    stub = _KBClientStub(active_after=1, sources=existing)
    monkeypatch.setattr(knowledge, "agent_client", lambda _ws=None: stub)
    monkeypatch.setattr(knowledge.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        knowledge,
        "_resolve_source",
        lambda _ws, _kb, _src: ("artifacts-bkt", "kb/KB123/"),
    )

    assert (
        knowledge._create_data_source(stub, ws_ctx(), "KB123", {"mode": "upload"})
        == "ds-old"
    )
    assert stub.created == []


def test_create_kb_returns_while_still_creating(monkeypatch):
    """#19: the create request must return immediately instead of polling
    toward ACTIVE — behind CloudFront's ~60 s origin timeout that wait cut the
    connection, the browser never ran its follow-up file upload, and the loss
    was silent. The background thread owns the whole data-source tail."""
    stub = _KBClientStub(active_after=99)  # never ACTIVE during the request
    stub.create_knowledge_base = lambda **_kw: {
        "knowledgeBase": {"knowledgeBaseId": "KB123", "status": "CREATING"}
    }
    monkeypatch.setattr(knowledge, "agent_client", lambda _ws=None: stub)
    monkeypatch.setattr(
        knowledge.time,
        "sleep",
        lambda _s: pytest.fail("create_kb must not sleep on the request path"),
    )
    started: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        knowledge,
        "_start_source_completion",
        lambda _ws, kb_id, src: started.append((kb_id, src)),
    )

    detail = knowledge.create_kb(
        ws_ctx({"kb_role_arn": "arn:aws:iam::111:role/kb"}), "docs", "", {"mode": "upload"}
    )

    assert detail["kb_id"] == "KB123"
    assert detail["status"] == "CREATING"
    assert detail["source_pending"] == {"mode": "upload"}
    assert started == [("KB123", {"mode": "upload"})]
    assert stub.created == []  # the data source is never created on-request


def test_source_completion_gives_up_when_kb_failed(monkeypatch):
    stub = _KBClientStub(active_after=99)
    stub.get_knowledge_base = lambda knowledgeBaseId: {  # noqa: N803
        "knowledgeBase": {"status": "FAILED"}
    }
    monkeypatch.setattr(knowledge, "agent_client", lambda _ws=None: stub)
    monkeypatch.setattr(knowledge.time, "sleep", lambda _s: None)

    thread = knowledge._start_source_completion(
        ws_ctx(), "KB123", {"mode": "upload"}, interval_s=0
    )
    thread.join(timeout=5)

    assert stub.created == []
