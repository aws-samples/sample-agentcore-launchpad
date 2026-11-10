"""Error-code envelope.

The backend returns machine-readable error codes; the frontend translates
them into the active locale. Envelope shape: {code, message, detail}.
"""

import logging
import re
from typing import Any

import urllib3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    HTTPClientError,
    IncompleteReadError,
    ReadTimeoutError,
)
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
)
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.display_time import DisplayTimeJSONResponse

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
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        if status_code is not None:
            self.status_code = status_code
        # Response headers the envelope must carry (r12-g: `Retry-After` on a 429).
        self.headers = headers


class NotFoundError(AppError):
    status_code = 404


def envelope(code: str, message: str, detail: Any = None) -> dict[str, Any]:
    return {"code": code, "message": message, "detail": detail}


async def app_error_handler(_: Request, exc: AppError) -> DisplayTimeJSONResponse:
    return DisplayTimeJSONResponse(
        status_code=exc.status_code,
        content=envelope(exc.code, exc.message, exc.detail),
        headers=exc.headers or None,
    )


async def http_error_handler(_: Request, exc: StarletteHTTPException) -> DisplayTimeJSONResponse:
    return DisplayTimeJSONResponse(
        status_code=exc.status_code,
        content=envelope(f"http.{exc.status_code}", str(exc.detail), None),
    )


# Field names whose value is a credential. A validation error located under one
# of these also loses its ``ctx`` (a custom validator's message may quote the
# value); ``input`` is never echoed at all (see validation_error_handler).
_SENSITIVE_FIELD_PARTS = (
    "secret",
    "password",
    "passwd",
    "token",
    "apikey",
    "api_key",
    "private_key",
    "privatekey",
    "authorization",
    "credential",
    "session_uri",
)


def is_sensitive_field(name: Any) -> bool:
    """``client_secret``, ``apiKey``, ``refresh_token``, ``Authorization``, … —
    case-insensitive, matched on the normalized field name."""
    if not isinstance(name, str):
        return False
    lowered = name.lower().replace("-", "_")
    compact = lowered.replace("_", "")
    return any(part in lowered or part in compact for part in _SENSITIVE_FIELD_PARTS)


def _redacted_validation_error(
    error: dict[str, Any], *, attachment_request: bool = False
) -> dict[str, Any]:
    """One Pydantic error row without the rejected input.

    Pydantic echoes ``input``: for a missing field that is the WHOLE request
    body (so a create-Connection 422 would carry its ``client_secret``), for a
    too-long field the value itself, and for an attachment megabytes of file
    data. The console never reads ``input`` — only ``loc``/``msg``/``ctx`` — so
    it is dropped for every error. ``ctx`` stays (the console shows its numeric
    limits) unless the error is located under a sensitive field or an
    attachment.
    """
    loc = tuple(error.get("loc", ()))
    dropped = {"input"}
    if (
        any(is_sensitive_field(part) for part in loc)
        or "attachments" in loc
        or (attachment_request and loc[0:1] == ("body",))
    ):
        dropped.add("ctx")
    return {k: v for k, v in error.items() if k not in dropped}


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> DisplayTimeJSONResponse:
    attachment_request = bool(request.scope.get("launchpad.attachment_request"))
    errors = [
        _redacted_validation_error(error, attachment_request=attachment_request)
        for error in exc.errors()
    ]
    return DisplayTimeJSONResponse(
        status_code=422,
        content=envelope(
            "validation.invalid_request",
            "Request validation failed",
            # ctx of custom validators may carry exception objects
            jsonable_encoder(errors, custom_encoder={Exception: str}),
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


async def client_error_handler(request: Request, exc: ClientError) -> DisplayTimeJSONResponse:
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
        return DisplayTimeJSONResponse(
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
        return DisplayTimeJSONResponse(
            status_code=status,
            content=envelope(
                code, _PUBLIC_AWS_MESSAGES[code], {"aws_error_code": aws_error_code(exc)}
            ),
        )
    return DisplayTimeJSONResponse(
        status_code=status,
        content=envelope(
            code,
            aws_error_message(exc),
            {"aws_error_code": aws_error_code(exc), "operation": operation},
        ),
    )


# N17-obs94: the botocore TRANSPORT family — what a call raises when AWS never answered
# (or the answer was cut) rather than answering with an error code: these are not
# `ClientError`s, so `client_error_handler` never saw them and every route whose AWS call
# timed out was a bare 500 with the endpoint URL (the runtime ARN inside) in the traceback.
# A timeout (the request was sent, nothing came back in time) is 504 `aws.timeout`; a
# connection that could not be made or was dropped (DNS / TCP / TLS / proxy failure, a
# closed or incomplete read) is 502 `aws.unreachable`. Both messages are static: the
# botocore text embeds the endpoint URL. Other `BotoCoreError`s (`ParamValidationError`,
# `NoCredentialsError`, …) are programming / configuration faults and stay loud (re-raised
# → 500), exactly like an unmapped `ClientError` code.
TRANSPORT_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = (ReadTimeoutError, ConnectTimeoutError)
TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    HTTPClientError,  # ReadTimeoutError, ConnectionClosedError, ResponseStreamingError
    BotoConnectionError,  # EndpointConnectionError, ConnectTimeoutError, SSLError, Proxy…
    IncompleteReadError,
)
TRANSPORT_ERROR_MESSAGES: dict[str, str] = {
    "aws.timeout": "AWS did not answer in time",
    "aws.unreachable": "AWS could not be reached",
}


def mapped_transport_error(exc: BaseException) -> tuple[int, str] | None:
    """`(status, code)` for a botocore transport exception, else None (not one)."""
    if isinstance(exc, TRANSPORT_TIMEOUT_ERRORS):
        return 504, "aws.timeout"
    if isinstance(exc, TRANSPORT_ERRORS):
        return 502, "aws.unreachable"
    return None


async def botocore_error_handler(request: Request, exc: BotoCoreError) -> DisplayTimeJSONResponse:
    """Turn a botocore transport exception into a 502 / 504 envelope — never a bare 500.

    Same shape on the console and on `/v1`: a static per-code message and
    `detail = {"error_type": "<botocore class>"}`; the raw text (which names the endpoint
    URL, i.e. the runtime ARN) goes to the log only. A `BotoCoreError` outside the
    transport family is re-raised: an unhandled 500 with the traceback in the log, where
    a programming fault belongs.
    """
    mapped = mapped_transport_error(exc)
    if mapped is None:
        raise exc
    status, code = mapped
    error_type = type(exc).__name__
    logger.info("aws transport %s → %s %s: %s", error_type, status, code, str(exc)[:600])
    return DisplayTimeJSONResponse(
        status_code=status,
        content=envelope(code, TRANSPORT_ERROR_MESSAGES[code], {"error_type": error_type}),
    )


# --- CN memory/KB (N6) fix round 2 ---
# OpenSearch statuses the console can translate (self-hosted Memory/KB
# substrate, N6 — there is no botocore client for its data plane, so
# `OpenSearchError` is this backend's `ClientError`). Anything outside this map
# keeps the pre-existing behaviour (re-raised → 500, loud in the log).
#
# N6 Task 3 fix round 1: `mapped_opensearch_error` is parameterized by `feature`
# ("memory" or "kb") so a knowledge-base request translates to `kb.*` and a memory
# request to `memory.*`, instead of always reading `memory.*` regardless of which
# feature's call actually failed. Both prefixes' messages live in the same flat
# dict — the codes never collide, so the lookup here (and in a router's own
# `_guard`, e.g. `routers/knowledge.py`) stays a single
# `OPENSEARCH_ERROR_MESSAGES[code]` regardless of feature.
OPENSEARCH_ERROR_MESSAGES: dict[str, str] = {
    "memory.backend_unavailable": (
        "the self-hosted memory/knowledge-base backend is temporarily "
        "unavailable — the OpenSearch domain is throttling or unhealthy"
    ),
    "memory.not_found": "the requested memory resource was not found",
    "kb.backend_unavailable": (
        "the self-hosted knowledge-base backend is temporarily unavailable — "
        "the OpenSearch domain is throttling or unhealthy"
    ),
    "kb.not_found": "the requested knowledge base resource was not found",
}


def mapped_opensearch_error(
    exc: BaseException, feature: str = "memory"
) -> tuple[int, str] | None:
    """`(status, code)` when the console has a translation for this
    `OpenSearchError`. 429 (throttled) and any 5xx (the domain itself unhealthy)
    map to 503 `<feature>.backend_unavailable`; 404 (a missing index/document) maps
    to `<feature>.not_found` — the same code the matching self-hosted backend
    (`memory_selfhosted.py` / `knowledge_selfhosted.py`) raises for a missing ledger
    row, so the console translates both the same way. `feature` defaults to
    `"memory"` — every pre-existing call site (routers/memory.py's `_guard`, the
    global handler below) keeps its exact prior behaviour unless it passes
    `feature="kb"` explicitly (N6 Task 3 fix round 1)."""
    from app.services.opensearch_http import OpenSearchError

    if not isinstance(exc, OpenSearchError):
        return None
    if exc.status == 404:
        return 404, f"{feature}.not_found"
    if exc.status == 429 or exc.status >= 500:
        return 503, f"{feature}.backend_unavailable"
    return None


def redacted_error(exc: BaseException) -> str:
    """One operator-facing line about a data-plane failure that can never carry a response
    body (N17-obs99 / N17-obs92): `OpenSearchError`'s own text embeds up to 500 chars of the
    raw OpenSearch answer (the IAM sentence with the role ARN, a `parsing_exception` root
    cause, record text), botocore's `An error occurred (…) when calling …` framing embeds the
    AWS message. The line is the exception type plus, for the two seam families, the status
    or the error code and the operation — nothing taken from a message.

    * `OpenSearchError` → `OpenSearchError: HTTP 403`
    * `ClientError` → `ClientError: SlowDown (ListObjectsV2)`
    * the transport family (urllib3 / socket, `BotoCoreError` — their text names pool
      internals and endpoint URLs) → the type name only
    * anything else (a local `ValueError` / `RuntimeError` — text this code wrote) → the
      type and the first line, at most 300 chars
    """
    from app.services.opensearch_http import OpenSearchError

    if isinstance(exc, OpenSearchError):
        return f"{type(exc).__name__}: HTTP {exc.status}"
    if isinstance(exc, ClientError):
        operation = getattr(exc, "operation_name", None) or "?"
        return f"{type(exc).__name__}: {aws_error_code(exc) or '?'} ({operation})"
    if isinstance(exc, (urllib3.exceptions.HTTPError, OSError, BotoCoreError)):
        return type(exc).__name__
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {text[:300]}" if text else type(exc).__name__


async def opensearch_error_handler(request: Request, exc: Exception) -> DisplayTimeJSONResponse:
    """Turn a self-hosted Memory/KB `OpenSearchError` the platform did not
    anticipate into a translatable envelope instead of a bare 500 — mirrors
    `client_error_handler`'s shape for AWS `ClientError`. The message is a
    static, per-code sentence (never `str(exc)`): `OpenSearchError`'s own text
    embeds up to 500 chars of the raw response body, which can echo back query
    or document content and must not reach a toast.

    Last-resort backstop (feature defaults to `"memory"`): every call in
    `routers/knowledge.py` already runs through that router's own `_guard`, which
    translates a mapped `OpenSearchError` into a `kb.*` `AppError` itself
    (`feature="kb"`) before it would ever reach this handler — so in practice only
    an *unguarded* `OpenSearchError` lands here (memory's own `_guard` re-raises on
    purpose; see `routers/memory.py`), and it is always memory-flavored.
    """
    mapped = mapped_opensearch_error(exc)
    if mapped is None:
        raise exc
    status, code = mapped
    logger.info("opensearch error %s → %s %s", exc.status, status, code)  # type: ignore[attr-defined]
    return DisplayTimeJSONResponse(
        status_code=status, content=envelope(code, OPENSEARCH_ERROR_MESSAGES[code])
    )
# --- end CN memory/KB (N6) fix round 2 ---


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(ClientError, client_error_handler)
    app.add_exception_handler(BotoCoreError, botocore_error_handler)  # N17-obs94
    # --- CN memory/KB (N6) fix round 2 ---
    from app.services.opensearch_http import OpenSearchError

    app.add_exception_handler(OpenSearchError, opensearch_error_handler)
    # --- end CN memory/KB (N6) fix round 2 ---
