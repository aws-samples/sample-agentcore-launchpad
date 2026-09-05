"""AWS `ClientError`s the platform did not anticipate become 4xx envelopes.

Hermetic: a throwaway FastAPI app with the platform's handlers and routes that
raise the `ClientError` a boto3 call would. No AWS, no app.main.
"""

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import AWS_ERROR_MAP, AppError, aws_error_message, register_error_handlers

BOTO_TEXT = "An error occurred (ResourceNotFoundException) when calling the GetMemory operation: "


def aws_error(code: str, message: str = "", operation: str = "GetRegistryRecord") -> ClientError:
    error: dict[str, str] = {"Code": code}
    if message:
        error["Message"] = message
    return ClientError({"Error": error}, operation)


def make_client() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/aws/{code}")
    def raise_aws(code: str, message: str = "", operation: str = "GetRegistryRecord"):
        raise aws_error(code, message, operation)

    @app.get("/service-mapped")
    def service_mapped():
        # What a service wrapper does when it anticipated the failure: its own
        # code must keep winning over the generic `aws.*` one.
        try:
            raise aws_error("ResourceNotFoundException", "no such kb", "GetKnowledgeBase")
        except ClientError as exc:
            raise AppError("kb.not_found", "Knowledge base not found", status_code=404) from exc

    @app.get("/v1/aws/{code}")
    def raise_aws_public(code: str, message: str = "", operation: str = "InvokeAgentRuntime"):
        raise aws_error(code, message, operation)

    @app.get("/assume-role-denied")
    def assume_role_denied():
        raise aws_error("AccessDenied", "not authorized to perform sts:AssumeRole", "AssumeRole")

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("aws_code", "status", "code"),
    [
        ("ResourceNotFoundException", 404, "aws.not_found"),
        ("ValidationException", 400, "aws.validation"),
        ("AccessDeniedException", 403, "aws.access_denied"),
        ("UnauthorizedException", 403, "aws.access_denied"),
        ("ThrottlingException", 429, "aws.throttled"),
        ("TooManyRequestsException", 429, "aws.throttled"),
        ("ServiceQuotaExceededException", 429, "aws.throttled"),
        ("ConflictException", 409, "aws.conflict"),
        ("ResourceInUseException", 409, "aws.conflict"),
        ("RetryableConflictException", 409, "aws.conflict"),
    ],
)
def test_mapped_client_errors_become_4xx_envelopes(aws_code, status, code):
    res = make_client().get(
        f"/aws/{aws_code}", params={"message": "Value at 'recordId' failed to satisfy constraint"}
    )
    assert res.status_code == status
    assert res.json() == {
        "code": code,
        "message": "Value at 'recordId' failed to satisfy constraint",
        "detail": {"aws_error_code": aws_code, "operation": "GetRegistryRecord"},
    }


def test_every_mapped_code_is_covered_by_the_parametrized_case():
    covered = {
        "ResourceNotFoundException", "ValidationException", "AccessDeniedException",
        "UnauthorizedException", "ThrottlingException", "TooManyRequestsException",
        "ServiceQuotaExceededException", "ConflictException", "ResourceInUseException",
        "RetryableConflictException",
    }  # fmt: skip
    assert set(AWS_ERROR_MAP) == covered


def test_message_never_carries_the_boto_prefix():
    # No `Message` in the response → botocore's str(exc) is the only text, and
    # it is framed as "An error occurred (…) when calling the … operation: …".
    res = make_client().get("/aws/ResourceNotFoundException", params={"operation": "GetMemory"})
    assert res.status_code == 404
    body = res.json()
    assert body["code"] == "aws.not_found"
    assert "An error occurred" not in body["message"]
    assert "GetMemory" not in body["message"]
    assert body["message"]  # never empty: falls back to the code text
    assert body["detail"] == {
        "aws_error_code": "ResourceNotFoundException",
        "operation": "GetMemory",
    }


def test_prefix_is_stripped_even_when_nested_in_the_message():
    # Some services relay a downstream ClientError's rendered text as their own
    # message; the prefix must go wherever it sits.
    exc = aws_error("ResourceNotFoundException", BOTO_TEXT + "Memory does-not-exist not found")
    assert aws_error_message(exc) == "Memory does-not-exist not found"


def test_unmapped_code_is_still_a_500():
    with pytest.raises(ClientError):
        TestClient(make_client().app, raise_server_exceptions=True).get(
            "/aws/InternalServerException"
        )
    res = make_client().get("/aws/InternalServerException")
    assert res.status_code == 500
    assert res.text == "Internal Server Error"


def test_service_level_mapping_takes_precedence():
    res = make_client().get("/service-mapped")
    assert res.status_code == 404
    assert res.json()["code"] == "kb.not_found"


def test_assume_role_failure_is_still_the_workspace_diagnostic():
    res = make_client().get("/assume-role-denied")
    assert res.status_code == 502
    body = res.json()
    assert body["code"] == "workspace.assume_role_failed"
    assert body["detail"] == {"aws_error_code": "AccessDenied"}


RAW_DENIED = (
    "User: arn:aws:sts::123456789012:assumed-role/admin_role/i-0abc is not authorized "
    "to perform: bedrock-agentcore:InvokeAgentRuntime"
)


def test_public_api_gets_a_generic_message_and_no_operation():
    """The same ClientError, two trust boundaries: the console operator sees the
    stripped AWS text; an API-key holder on /v1 must not learn the deployment's
    role ARN, instance id or boto operation."""
    client = make_client()
    params = {"message": RAW_DENIED, "operation": "InvokeAgentRuntime"}

    console = client.get("/aws/AccessDeniedException", params=params)
    assert console.status_code == 403
    assert console.json() == {
        "code": "aws.access_denied",
        "message": RAW_DENIED,
        "detail": {"aws_error_code": "AccessDeniedException", "operation": "InvokeAgentRuntime"},
    }

    public = client.get("/v1/aws/AccessDeniedException", params=params)
    assert public.status_code == 403
    body = public.json()
    assert body == {
        "code": "aws.access_denied",
        "message": "AWS access denied",
        "detail": {"aws_error_code": "AccessDeniedException"},
    }
    assert "arn:aws" not in public.text and "i-0abc" not in public.text
    assert "operation" not in public.text and "InvokeAgentRuntime" not in public.text


@pytest.mark.parametrize(
    ("aws_code", "status", "code", "message"),
    [
        ("ResourceNotFoundException", 404, "aws.not_found", "AWS resource not found"),
        ("ValidationException", 400, "aws.validation", "AWS rejected the request as invalid"),
        ("UnauthorizedException", 403, "aws.access_denied", "AWS access denied"),
        ("ThrottlingException", 429, "aws.throttled", "AWS is throttling this request"),
        ("ConflictException", 409, "aws.conflict", "AWS resource conflict"),
    ],
)
def test_every_public_code_has_a_generic_message(aws_code, status, code, message):
    res = make_client().get(f"/v1/aws/{aws_code}", params={"message": "secret detail"})
    assert res.status_code == status
    assert res.json() == {
        "code": code,
        "message": message,
        "detail": {"aws_error_code": aws_code},
    }
