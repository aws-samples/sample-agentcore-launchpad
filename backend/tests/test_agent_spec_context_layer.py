"""``AgentSpec.context_layer``: where it may sit, the reserved tool name, the stored
shape of every other spec, and the request-time "is a layer connected" check."""

import pytest
from pydantic import ValidationError

from app.routers import agents as agents_router
from app.schemas.agent import AgentSpec

from . import coa_fakes as fx

REF = {"namespace_id": fx.NAMESPACE, "namespace_name": "AnyCompany HR"}


def _spec(**fields):
    base = {"name": "hr-agent", "method": "harness", "system_prompt": "help", "context_layer": REF}
    return AgentSpec(**{**base, **fields})


@pytest.mark.parametrize("fields", [
    {"method": "harness"},
    {"method": "zip_runtime"},
    {"method": "zip_runtime", "protocol": "http"},
])
def test_supported_combinations(fields):
    assert _spec(**fields).context_layer.namespace_id == fx.NAMESPACE


@pytest.mark.parametrize("fields,fragment", [
    ({"method": "container"}, "container method"),
    ({"method": "studio"}, "studio method"),
    ({"method": "zip_runtime", "protocol": "a2a"}, "protocol=a2a"),
    ({"method": "zip_runtime", "code": "print(1)"}, "code/code_bundle"),
    ({"method": "zip_runtime", "code_bundle": {"main.py": "x"}}, "code/code_bundle"),
    ({"tools": [{"type": "mcp", "name": "coa", "config": {"url": "https://x/mcp"}}]}, "reserved"),
])
def test_unsupported_combinations_are_refused_by_name(fields, fragment):
    with pytest.raises(ValidationError) as exc:
        _spec(**fields)
    error = next(e for e in exc.value.errors() if e["type"] == "context_layer.unsupported_method")
    assert fragment in error["msg"]


def test_byoc_cannot_attach_a_layer():
    with pytest.raises(ValidationError) as exc:
        AgentSpec(
            name="byo-agent", method="byoc", context_layer=REF,
            byoc={"artifact_kind": "container_image",
                  "image_uri": "111122223333.dkr.ecr.us-west-2.amazonaws.com/a:1"},
        )
    assert "context_layer.unsupported_method" in {e["type"] for e in exc.value.errors()}


@pytest.mark.parametrize("namespace_id", ["", "not-a-uuid", "dc09046b28af4c7ba03a4fa63cda243b"])
def test_the_namespace_id_is_a_uuid(namespace_id):
    with pytest.raises(ValidationError):
        _spec(context_layer={"namespace_id": namespace_id})


def test_a_spec_without_a_layer_keeps_its_stored_shape():
    spec = AgentSpec(name="plain-agent", method="harness", system_prompt="hi")
    assert "context_layer" not in spec.model_dump()
    assert _spec().model_dump()["context_layer"] == REF


# ── request-time: a layer must be connected ──────────────────────────────────


@pytest.fixture
def launched(monkeypatch):
    jobs: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: jobs.append(jid))
    return jobs


def _body(**fields):
    return {"name": "hr-agent", "method": "harness", "system_prompt": "help",
            "context_layer": REF, **fields}


def test_create_is_refused_when_no_layer_is_connected(client, launched, monkeypatch):
    fx.install(monkeypatch)
    response = client.post("/api/agents", json=_body())
    assert response.status_code == 409
    assert response.json()["code"] == "context_layer.not_connected"
    assert launched == []


def test_create_is_accepted_once_a_layer_is_connected(client, launched, monkeypatch):
    account = fx.install(monkeypatch)
    fx.connect_default(account)
    response = client.post("/api/agents", json=_body())
    assert response.status_code == 202, response.text
    assert response.json()["agent"]["spec"]["context_layer"] == REF
    assert len(launched) == 1


def test_create_on_an_unsupported_method_answers_422(client, launched, monkeypatch):
    fx.install(monkeypatch)
    response = client.post("/api/agents", json=_body(method="container"))
    assert response.status_code == 422
    assert "context_layer.unsupported_method" in {e["type"] for e in response.json()["detail"]}
