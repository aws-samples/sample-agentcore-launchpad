"""Error-code envelope.

The backend returns machine-readable error codes; the frontend translates
them into the active locale. Envelope shape: {code, message, detail}; the
console's AWS-error envelopes add ``request_id`` (projects §19), and a console
``AppError`` may add ``next`` (where the console sends the user to fix it) and
``needs`` (the permission that can) — storyline-ux §4, guided-console §3.1.
"""

import logging
import re
import uuid
from typing import Any, TypedDict

from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("launchpad.errors")


class NextStep(TypedDict):
    """One way out of an error: a console action key and its parameters.

    The backend names the action only; the console's action table
    (``frontend/src/v3/story/actions.ts``) owns the route, so a page redesign never
    touches the backend. An action the console does not know is ignored there."""

    action: str
    params: dict[str, str]


def next_step(action: str, **params: str | None) -> NextStep:
    """``NextStep`` with the empty parameters dropped (an id the caller lacks)."""
    return {"action": action, "params": {k: str(v) for k, v in params.items() if v}}


class AppError(Exception):
    """Domain error carrying a stable error code for frontend translation.

    ``next`` (first entry = the primary action) and ``needs`` (``perm:<key>`` when
    only that permission can fix it) reach the console envelope only — the public
    ``/v1`` API and the ``/share`` pages never carry them (``app_error_handler``)."""

    status_code = 400

    def __init__(
        self,
        code: str,
        message: str,
        detail: Any = None,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
        *,
        next: list[NextStep] | None = None,  # noqa: A002 - the envelope's field name
        needs: str | None = None,
    ):
        super().__init__(message)
        self.headers = headers
        self.code = code
        self.message = message
        self.detail = detail
        self.next = list(next) if next else None
        self.needs = needs
        if status_code is not None:
            self.status_code = status_code


class NotFoundError(AppError):
    status_code = 404


def envelope(
    code: str,
    message: str,
    detail: Any = None,
    request_id: str | None = None,
    *,
    next: list[NextStep] | None = None,  # noqa: A002 - the envelope's field name
    needs: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message, "detail": detail}
    if request_id is not None:
        body["request_id"] = request_id
    if next:
        body["next"] = next
    if needs:
        body["needs"] = needs
    return body


def is_public_path(path: str) -> bool:
    """``/v1`` (API-key callers) and ``/share`` (account-free pages): across the trust
    boundary, where console routes and permission names mean nothing to the caller."""
    return path.startswith((PUBLIC_API_PREFIX, PUBLIC_SHARE_PREFIX))


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    guided = not is_public_path(request.url.path)
    return JSONResponse(
        status_code=exc.status_code,
        content=envelope(
            exc.code, exc.message, exc.detail,
            next=getattr(exc, "next", None) if guided else None,
            needs=getattr(exc, "needs", None) if guided else None,
        ),
        headers=getattr(exc, "headers", None),
    )


async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
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


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    attachment_request = bool(request.scope.get("launchpad.attachment_request"))
    errors = [
        _redacted_validation_error(error, attachment_request=attachment_request)
        for error in exc.errors()
    ]
    return JSONResponse(
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
# the account-free share pages (T13) sit across the same boundary
PUBLIC_SHARE_PREFIX = "/share"
_PUBLIC_AWS_MESSAGES: dict[str, str] = {
    "aws.not_found": "AWS resource not found",
    "aws.validation": "AWS rejected the request as invalid",
    "aws.access_denied": "AWS access denied",
    "aws.throttled": "AWS is throttling this request",
    "aws.conflict": "AWS resource conflict",
    "aws.name_taken": "An AWS resource with this name already exists",
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
    code = aws_error_code(exc)
    if code == "ConflictException" and _NAME_TAKEN.search(aws_error_message(exc)):
        return (409, "aws.name_taken")
    return AWS_ERROR_MAP.get(code)


# A create refused because the NAME is held: evaluators, knowledge bases, policy
# engines, gateways… are unique per account + Region, not per project, and the
# holder may live in another project or be an earlier event's leftover the
# console has no record of. "Busy, retry once it settles" (aws.conflict) would
# send the operator into retries that can never succeed. AWS words it
# "Evaluator with same name already exist" / "KnowledgeBase with name X already
# exists." / "… already exists".
_NAME_TAKEN = re.compile(r"already exist|same name", re.IGNORECASE)


# The "it is already gone" family across the services a best-effort cleanup
# deletes from (AgentCore, IAM, S3, ECR). Only these may be swallowed silently.
NOT_FOUND_CODES = frozenset({
    "ResourceNotFoundException",
    "NotFoundException",
    "NoSuchEntity",
    "NoSuchKey",
    "ImageNotFoundException",
    "ImageNotFound",
    "RepositoryNotFoundException",
})


def is_not_found(exc: BaseException) -> bool:
    """True when `exc` is AWS saying the resource does not exist.

    A best-effort delete swallows exactly this; anything else (AccessDenied,
    throttling, validation, a bug) leaves a resource behind and must be logged."""
    return isinstance(exc, ClientError) and aws_error_code(exc) in NOT_FOUND_CODES


def cleanup_error(exc: BaseException) -> str:
    """`Code: message` for a failed cleanup call — the AWS error code when there
    is one, else the exception type — for the warning that names what was left."""
    if isinstance(exc, ClientError):
        return f"{aws_error_code(exc) or 'ClientError'}: {aws_error_message(exc)}"
    return f"{type(exc).__name__}: {exc}"


REQUEST_ID_HEADER = "X-Request-Id"
# what a proxy in front of the backend may already have stamped on the request;
# anything longer or with other characters is not reused (it is echoed into logs)
_REQUEST_ID_SHAPE = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def request_id_for(request: Request) -> str:
    """The id that ties a console error to its log line.

    Reuses an inbound ``X-Request-Id`` when a proxy set one, else mints a short
    random id; ``journalctl -u launchpad-backend | grep <id>`` finds the line."""
    inbound = request.headers.get(REQUEST_ID_HEADER, "")
    if _REQUEST_ID_SHAPE.fullmatch(inbound):
        return inbound
    return uuid.uuid4().hex[:16]


def aws_request_id(exc: ClientError) -> str | None:
    """AWS's own request id for the failed call — what AWS Support asks for."""
    meta = exc.response.get("ResponseMetadata", {}) if isinstance(exc.response, dict) else {}
    value = meta.get("RequestId") if isinstance(meta, dict) else None
    return str(value) if value else None


# 12-digit account ids, standalone or inside an ARN (`arn:aws:iam::<acct>:role/…`).
_ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")


def mask_account_ids(text: str) -> str:
    """``123456789012`` → ``********9012`` wherever it appears — enough to tell two
    accounts apart, not enough to name one."""
    return _ACCOUNT_ID.sub(lambda m: "*" * 8 + m.group(0)[-4:], text)


def _caller_is_admin(request: Request) -> bool:
    """Admins see AWS text in full, members get account ids masked (projects §19.3.5).

    No identity on the request means the auth gate is off — the console is the
    local operator's, treated as the built-in admin everywhere else too."""
    identity = getattr(request.state, "identity", None)
    if identity is None:
        return True
    return bool(getattr(identity, "is_admin", False))


def public_aws_message(code: str) -> str:
    """The generic sentence for a mapped `aws.*` code — no ARN, account id or operation."""
    return _PUBLIC_AWS_MESSAGES.get(code, "AWS request failed")


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
    API-key holder has no business seeing. That envelope is unchanged by §19.

    Console envelopes carry ``request_id`` (also in the log line and the
    ``X-Request-Id`` response header) and, when AWS returned one, the call's
    ``aws_request_id`` in ``detail``. A member's copy has account ids masked and
    ``detail.masked`` set; an admin's is the AWS text verbatim.
    """
    # Imported here: `core` must not take a startup dependency on `services`.
    from app.services.aws_clients import assume_role_diagnostic, is_assume_role_failure

    if is_assume_role_failure(exc):
        request_id = request_id_for(request)
        logger.info(
            "aws %s on AssumeRole → 502 workspace.assume_role_failed [request %s]",
            aws_error_code(exc),
            request_id,
        )
        return JSONResponse(
            status_code=502,
            content=envelope(
                "workspace.assume_role_failed",
                assume_role_diagnostic(exc),
                {"aws_error_code": aws_error_code(exc)},
                request_id,
            ),
            headers={REQUEST_ID_HEADER: request_id},
        )
    mapped = mapped_aws_error(exc)
    if mapped is None:
        raise exc
    status, code = mapped
    operation = exc.operation_name or ""
    public = request.url.path.startswith((PUBLIC_API_PREFIX, PUBLIC_SHARE_PREFIX))
    request_id = None if public else request_id_for(request)
    logger.info(
        "aws %s on %s → %s %s: %s%s",
        aws_error_code(exc),
        operation,
        status,
        code,
        aws_error_message(exc),
        f" [request {request_id}]" if request_id else "",
    )
    if public:
        return JSONResponse(
            status_code=status,
            content=envelope(
                code, _PUBLIC_AWS_MESSAGES[code], {"aws_error_code": aws_error_code(exc)}
            ),
        )
    message = aws_error_message(exc)
    detail: dict[str, Any] = {"aws_error_code": aws_error_code(exc), "operation": operation}
    if aws_id := aws_request_id(exc):
        detail["aws_request_id"] = aws_id
    if not _caller_is_admin(request):
        masked = mask_account_ids(message)
        if masked != message:
            message = masked
            detail["masked"] = True
    return JSONResponse(
        status_code=status,
        content=envelope(code, message, detail, request_id),
        headers={REQUEST_ID_HEADER: request_id} if request_id else None,
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(ClientError, client_error_handler)
