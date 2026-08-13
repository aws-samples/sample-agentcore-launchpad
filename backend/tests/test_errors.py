import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import AppError, NotFoundError, register_error_handlers


def make_client() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/boom")
    def boom():
        raise AppError("agent.invalid_state", "Agent is not deployable", {"agent_id": "a-1"})

    @app.get("/missing")
    def missing():
        raise NotFoundError("agent.not_found", "Agent not found")

    @app.get("/assume-role-denied")
    def assume_role_denied():
        # What a request signing against a cross-account workspace raises when the
        # spoke's trust policy or its ExternalId does not match: the credential
        # refresh is lazy, so it detonates inside an unrelated handler.
        raise ClientError(
            {
                "Error": {
                    "Code": "AccessDenied",
                    "Message": "not authorized to perform sts:AssumeRole",
                }
            },
            "AssumeRole",
        )

    @app.get("/aws-denied")
    def aws_denied():
        raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "CreateGateway")

    return TestClient(app, raise_server_exceptions=False)


def test_app_error_envelope():
    res = make_client().get("/boom")
    assert res.status_code == 400
    assert res.json() == {
        "code": "agent.invalid_state",
        "message": "Agent is not deployable",
        "detail": {"agent_id": "a-1"},
    }


def test_not_found_status():
    res = make_client().get("/missing")
    assert res.status_code == 404
    assert res.json()["code"] == "agent.not_found"


def test_unknown_route_uses_http_envelope():
    res = make_client().get("/nope")
    assert res.status_code == 404
    assert res.json()["code"] == "http.404"


def test_a_failed_role_assumption_is_a_translatable_error_not_a_500():
    res = make_client().get("/assume-role-denied")
    assert res.status_code == 502
    body = res.json()
    assert body["code"] == "workspace.assume_role_failed"
    assert "trust policy" in body["message"] and "ExternalId" in body["message"]
    assert body["detail"] == {"aws_error_code": "AccessDenied"}


def test_other_aws_errors_are_left_alone():
    """The net is deliberately narrow: only STS AssumeRole is a credential
    problem. Everything else stays the unhandled failure it was."""
    with pytest.raises(ClientError):
        TestClient(make_client().app, raise_server_exceptions=True).get("/aws-denied")
    assert make_client().get("/aws-denied").status_code == 500
