"""The Context Layer module's COA reads (`/api/context-layer/namespaces/{id}/*`,
`/health`): each is made as the member, comes back as an allowlisted projection,
and maps COA's refusals to the ``context_layer.*`` codes.

Hermetic: AWS is ``coa_fakes.Account``, COA's REST API is ``coa_fakes.CoaHttp``
behind ``httpx.MockTransport``, and the shapes are trimmed from the live probes.
"""

import time

import httpx
import pytest

from app.services import context_layer_api
from app.services import context_layer_tokens as tokens

from . import coa_fakes as fx

NS = fx.NAMESPACE
BASE = f"/api/context-layer/namespaces/{NS}"
READS = [BASE, f"{BASE}/sources", f"{BASE}/metrics", f"{BASE}/ontology"]


@pytest.fixture
def account(monkeypatch):
    account = fx.install(monkeypatch)
    fx.connect_default(account)
    account.data.token = fx.jwt(time.time() + 3600, sub="u-1")
    return account


@pytest.fixture
def coa(monkeypatch):
    return fx.install_http(monkeypatch)


def _no_token(response, account):
    assert account.data.token not in response.text
    assert "Bearer" not in response.text


def test_the_namespace_is_read_as_the_member_and_projected(client, account, coa):
    response = client.get(BASE)
    assert response.status_code == 200, response.text
    ns = response.json()["namespace"]
    assert ns == {
        "namespace_id": NS, "name": "anycompany-hr", "display_name": "AnyCompany HR",
        "description": "HR policies and leave data", "owner": "owner@example.com",
        "status": "ACTIVE", "source_count": 2, "created_at": "2026-10-10T08:00:00Z",
        "updated_at": "2026-10-18T09:30:00Z",
    }
    request = coa.requests[-1]
    assert str(request.url) == f"{fx.API_ENDPOINT}namespaces/{NS}"
    assert request.headers["authorization"] == f"Bearer {account.data.token}"
    _no_token(response, account)


def test_sources_page_through_and_turn_epoch_times_into_iso(client, account, coa):
    response = client.get(f"{BASE}/sources")
    assert response.status_code == 200, response.text
    sources = response.json()["sources"]
    assert [s["source_id"] for s in sources] == [fx.SOURCE_DOCS, fx.SOURCE_GLUE]
    docs, glue = sources
    assert docs == {
        "source_id": fx.SOURCE_DOCS, "name": "HR policy documents", "source_type": "DOCUMENTS",
        "source_subtype": "S3_DOCUMENTS", "status": "COMPLETED", "tables_discovered": None,
        "created_at": "2025-10-10T08:00:00+00:00", "updated_at": "2025-10-18T08:00:00+00:00",
    }
    assert glue["tables_discovered"] == 3 and glue["updated_at"] is None
    assert "configuration" not in response.text and "never-projected" not in response.text
    assert [r.url.params.get("nextToken") for r in coa.requests] == [None, "page-2"]
    assert coa.requests[0].url.params["maxResults"] == "100"


def test_metrics_keep_the_governed_definition_and_drop_the_rest(client, account, coa):
    response = client.get(f"{BASE}/metrics")
    assert response.status_code == 200, response.text
    (metric,) = response.json()["metrics"]
    assert metric == {
        "name": "available_annual_leave_days",
        "description": "Annual leave still bookable: remaining days minus pending requests.",
        "unit": "days", "return_type": "decimal", "default_time_grain": None,
        "synonyms": ["可用年假", "leave left"],
        "dialects": [{"dialect": "ATHENA",
                      "expression": "SUM(remaining_days) - SUM(pending_days)"}],
        "source_table": "leave_balances", "data_source_id": fx.SOURCE_GLUE,
    }
    assert "definedBy" not in response.text and "not projected" not in response.text


def test_ontology_lists_induced_first_with_every_ontologys_classes(client, account, coa):
    response = client.get(f"{BASE}/ontology")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [o["title"] for o in body["ontologies"]] == ["AnyCompany HR", "Foundation"]
    assert body["ontologies"][0]["status"] == "ok"  # the ingest marker when no lifecycle
    labels = [(c["label"], c["ontology"]) for c in body["classes"]]
    assert labels == [
        ("employees", "AnyCompany HR"), ("leave_balances", "AnyCompany HR"),
        ("Person", "Foundation"),  # no rdfs:label → the IRI's local name
    ]
    assert body["classes"][0]["description"] == "People employed by AnyCompany."
    assert body["failed"] == [] and body["truncated"] is False
    overviews = [r for r in coa.requests if r.url.path.endswith("ontology-overview")]
    # the IRI (with its '#') rides the query string, never the path
    assert {r.url.params["ontology_id"] for r in overviews} == {
        fx.ONTOLOGY_INDUCED, fx.ONTOLOGY_BASE,
    }
    assert "graphUri" not in response.text and "urn:graph" not in response.text


def test_an_overview_coa_cannot_answer_is_listed_and_the_rest_still_show(
    client, account, coa,
):
    def overview(request):
        if request.url.params["ontology_id"] == fx.ONTOLOGY_BASE:
            return httpx.Response(502, json={"message": "Bad gateway"})
        return httpx.Response(200, json=fx.OVERVIEWS[fx.ONTOLOGY_INDUCED])

    coa.overrides["graph/ontology-overview"] = overview
    body = client.get(f"{BASE}/ontology").json()
    assert body["failed"] == [fx.ONTOLOGY_BASE]
    assert [c["label"] for c in body["classes"]] == ["employees", "leave_balances"]


def test_a_large_ontology_reads_as_truncated(client, account, coa, monkeypatch):
    monkeypatch.setattr(context_layer_api, "CLASSES_PER_ONTOLOGY", 1)
    body = client.get(f"{BASE}/ontology").json()
    assert body["truncated"] is True


@pytest.mark.parametrize("path", READS)
def test_every_read_needs_consent_first(client, account, coa, path):
    account.data.token = None
    response = client.get(path)
    assert response.status_code == 409
    assert response.json()["code"] == "context_layer.authorization_required"
    assert coa.requests == []


@pytest.mark.parametrize("path", READS)
@pytest.mark.parametrize("status,code,http", [
    (401, "context_layer.reauthorize", 409),
    (403, "context_layer.forbidden", 403),
    (404, "context_layer.coa_not_found", 404),
    (500, "context_layer.upstream", 502),
    (429, "context_layer.upstream", 502),
])
def test_every_read_maps_coa_refusals(client, account, coa, path, status, code, http):
    coa.overrides.update({
        key: httpx.Response(status, json={"message": "x" * 500})
        for key in ("namespace", "sources", "metrics", "ontologies")
    })
    response = client.get(path)
    assert response.status_code == http, response.text
    body = response.json()
    assert body["code"] == code
    _no_token(response, account)
    if status == 401:
        assert ("default", "admin") in tokens._FORCE
    else:
        # COA's own message, bounded to 200 characters, never the whole body
        assert len(body["detail"]["coa_message"]) <= context_layer_api.COA_MESSAGE_MAX + 1


@pytest.mark.parametrize("path", READS)
def test_every_read_is_refused_when_nothing_is_connected(client, monkeypatch, coa, path):
    fx.install(monkeypatch)
    response = client.get(path)
    assert response.status_code == 409
    assert response.json()["code"] == "context_layer.not_connected"


@pytest.mark.parametrize("suffix", ["", "/sources", "/metrics", "/ontology"])
def test_a_namespace_id_that_is_not_a_uuid_never_reaches_coa(client, account, coa, suffix):
    response = client.get(f"/api/context-layer/namespaces/not-a-uuid{suffix}")
    assert response.status_code == 422
    assert coa.requests == []


def test_a_stored_endpoint_that_is_not_https_never_gets_the_bearer(client, account, coa):
    from app.core.db import SessionLocal
    from app.models.ledger import ContextLayer as ContextLayerRow

    db = SessionLocal()
    try:
        row = db.query(ContextLayerRow).one()
        row.resources = {**row.resources, "api_endpoint": "http://coa.example/prod/"}
        db.commit()
    finally:
        db.close()
    response = client.get(BASE)
    assert response.status_code == 502
    assert response.json()["code"] == "context_layer.upstream"
    assert coa.requests == []


def test_namespace_path_refuses_anything_but_a_uuid():
    with pytest.raises(context_layer_api.AppError) as exc:
        context_layer_api.namespace_path("../grants")
    assert exc.value.code == "context_layer.invalid_namespace"
    assert context_layer_api.namespace_path(NS, "metrics") == f"namespaces/{NS}/metrics"


# ── health ───────────────────────────────────────────────────────────────────


def test_health_combines_runtime_status_with_coa_system_health(client, account, coa):
    account.data.token = None
    client.post("/api/context-layer/authorize")  # pending → not yet authorized
    blocked = client.get("/api/context-layer/health").json()
    assert blocked["authorization_required"] is True and blocked["coa"] is None
    assert blocked["runtimes"] == {"mcp": "READY", "serve": "READY"}
    assert coa.requests == []  # COA is never called without consent

    _authorize(account)
    response = client.get("/api/context-layer/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ready"] is True and body["authorization_required"] is False
    assert body["coa"] == {"status": "degraded", "components": [
        {"key": "dynamodb", "status": "ok"},
        {"key": "neptune_kg", "status": "ok"},
        {"key": "opensearch", "status": "error: collection unreachable"},
        {"key": "graph_store", "status": "ok"},
        {"key": "vector_store", "status": "ok"},
        {"key": "bedrock_embeddings", "status": "ok"},
        {"key": "description_llm", "status": "degraded"},
    ]}
    assert "never-projected" not in response.text
    # runtime status is read in the COA region, with the platform's IAM identity
    assert account.regions_of("bedrock-agentcore-control") >= {fx.COA_REGION}


def test_health_reports_a_coa_refusal_beside_the_runtime_status(client, account, coa):
    _authorize(account)
    coa.overrides["system-health"] = httpx.Response(403, json={"message": "no"})
    body = client.get("/api/context-layer/health").json()
    assert body["coa"] is None
    assert body["coa_error"] == {"code": "context_layer.forbidden", "status": 403}
    assert body["runtimes"]["serve"] == "READY"


def test_health_degrades_a_runtime_probe_to_unknown(client, account, coa):
    def boom(agentRuntimeId):
        raise fx.client_error("AccessDeniedException")

    account.control.get_agent_runtime = boom
    body = client.get("/api/context-layer/health").json()
    assert body["runtimes"] == {"mcp": None, "serve": None} and body["ready"] is False
    assert {e["code"] for e in body["probe_errors"]} == {"AccessDeniedException"}


def _authorize(account) -> None:
    """Complete the admin's console consent so the ledger reads ``authorized``."""
    from app.core.db import SessionLocal
    from app.services import oauth_sessions
    from app.services.workspace import context_for_workspace

    token, account.data.token = account.data.token, None
    db = SessionLocal()
    try:
        tokens.user_token(db, context_for_workspace("default"), "admin")
        oauth_sessions.complete(db, "default", "urn:session:abc", caller="admin",
                                data_client=account.data)
    finally:
        db.close()
    account.data.token = token or fx.jwt(time.time() + 3600, sub="u-1")
