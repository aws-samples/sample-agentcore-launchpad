"""Error-code envelope.

The backend returns machine-readable error codes; the frontend translates
them into the active locale. Envelope shape: {code, message, detail}.
"""

import logging
import re
from typing import Any

from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("launchpad.errors")


class AppError(Exception):
    """Domain error carrying a stable error code for frontend translation."""

    status_code = 400

    def __init__(
        self,
        code: str,
        message: str,
        detail: Any = None,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        if status_code is not None:
            self.status_code = status_code


class NotFoundError(AppError):
    status_code = 404


def envelope(code: str, message: str, detail: Any = None) -> dict[str, Any]:
    return {"code": code, "message": message, "detail": detail}


async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=envelope(exc.code, exc.message, exc.detail),
    )


async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=envelope(f"http.{exc.status_code}", str(exc.detail), None),
    )


async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=envelope(
            "validation.invalid_request",
            "Request validation failed",
            # ctx of custom validators may carry exception objects
            jsonable_encoder(exc.errors(), custom_encoder={Exception: str}),
        ),
    )


# AWS error codes the console can translate. The status is what the same
# condition would be on a REST API; the code is the `apiErrors.*` key the console
# maps to localized copy. Anything not listed keeps the pre-existing behaviour
# (re-raised → 500 with the traceback in the log) so a genuinely unexpected AWS
# failure is still loud.
AWS_ERROR_MAP: dict[str, tuple[int, str]] = {
    "ResourceNotFoundException": (404, "aws.not_found"),
    "ValidationException": (400, "aws.validation"),
    "AccessDeniedException": (403, "aws.access_denied"),
    "UnauthorizedException": (403, "aws.access_denied"),
    "ThrottlingException": (429, "aws.throttled"),
    "TooManyRequestsException": (429, "aws.throttled"),
    "ServiceQuotaExceededException": (429, "aws.throttled"),
    "ConflictException": (409, "aws.conflict"),
    "ResourceInUseException": (409, "aws.conflict"),
    # Runtime data plane (StopRuntimeSession): transient, retried by botocore
    # first; only what survives the retries reaches the console.
    "RetryableConflictException": (409, "aws.conflict"),
}

# What an API-key consumer on `/v1` sees instead of the AWS message. The raw text
# names this deployment's role ARN, instance id and operation — fine for the
# console operator, a leak across the API-key trust boundary.
PUBLIC_API_PREFIX = "/v1"
_PUBLIC_AWS_MESSAGES: dict[str, str] = {
    "aws.not_found": "AWS resource not found",
    "aws.validation": "AWS rejected the request as invalid",
    "aws.access_denied": "AWS access denied",
    "aws.throttled": "AWS is throttling this request",
    "aws.conflict": "AWS resource conflict",
}

# botocore renders `str(exc)` as
# "An error occurred (Code) when calling the Op operation: Message" — that
# prefix is what this handler exists to keep out of the console.
_BOTO_PREFIX = re.compile(r"An error occurred \([\w.]+\) when calling the \w+ operation: ?")


def aws_error_code(exc: ClientError) -> str:
    error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
    return str(error.get("Code") or "")


def aws_error_message(exc: ClientError) -> str:
    """The AWS message without botocore's `An error occurred (…)` framing.

    Falls back to `str(exc)` (prefix stripped) when the response carries no
    message, so the envelope never reads as an empty string.
    """
    error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
    message = str(error.get("Message") or "") or str(exc)
    return _BOTO_PREFIX.sub("", message).strip()


def mapped_aws_error(exc: BaseException) -> tuple[int, str] | None:
    """`(status, code)` when the console has a translation for this `ClientError`."""
    if not isinstance(exc, ClientError):
        return None
    return AWS_ERROR_MAP.get(aws_error_code(exc))


async def client_error_handler(request: Request, exc: ClientError) -> JSONResponse:
    """Turn an AWS `ClientError` the platform did not anticipate into a 4xx envelope.

    Every boto3 client is built in one place, but the calls are spread over every
    router, so a wrong id, an IAM gap or a throttle detonates at whichever call
    site was signing a request — not one place worth try/excepting. Handled here
    so the console gets a code it can translate instead of a bare 500 or boto's
    `An error occurred (…)` text. Service-level mappings (`kb.not_found`, …) still
    win: they raise `AppError` before the `ClientError` reaches this handler.

    A failed cross-account role assumption is checked first — STS reports it as a
    plain `AccessDenied` on `AssumeRole`, which is a workspace-credential problem
    with its own diagnostic, not an access-denied answer from the target service.

    Codes outside `AWS_ERROR_MAP` are re-raised, which leaves them exactly where
    they were before: an unhandled 500 with the AWS error in the log.

    On the public `/v1` API the status and code are kept but the message is a
    generic per-code sentence and `detail` carries only `aws_error_code`: the raw
    AWS text names the deployment's role ARN, instance id and operation, which an
    API-key holder has no business seeing.
    """
    # Imported here: `core` must not take a startup dependency on `services`.
    from app.services.aws_clients import assume_role_diagnostic, is_assume_role_failure

    if is_assume_role_failure(exc):
        return JSONResponse(
            status_code=502,
            content=envelope(
                "workspace.assume_role_failed",
                assume_role_diagnostic(exc),
                {"aws_error_code": aws_error_code(exc)},
            ),
        )
    mapped = mapped_aws_error(exc)
    if mapped is None:
        raise exc
    status, code = mapped
    operation = exc.operation_name or ""
    logger.info(
        "aws %s on %s → %s %s: %s",
        aws_error_code(exc),
        operation,
        status,
        code,
        aws_error_message(exc),
    )
    if request.url.path.startswith(PUBLIC_API_PREFIX):
        return JSONResponse(
            status_code=status,
            content=envelope(
                code, _PUBLIC_AWS_MESSAGES[code], {"aws_error_code": aws_error_code(exc)}
            ),
        )
    return JSONResponse(
        status_code=status,
        content=envelope(
            code,
            aws_error_message(exc),
            {"aws_error_code": aws_error_code(exc), "operation": operation},
        ),
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(ClientError, client_error_handler)
