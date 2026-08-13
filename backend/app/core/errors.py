"""Error-code envelope.

The backend returns machine-readable error codes; the frontend translates
them into the active locale. Envelope shape: {code, message, detail}.
"""

from typing import Any

from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


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


async def assume_role_error_handler(_: Request, exc: ClientError) -> JSONResponse:
    """Turn a failed cross-account role assumption into an actionable answer.

    A workspace's credentials are refreshed lazily, so a broken trust policy or a
    wrong ExternalId detonates at whichever call site was signing a request — any
    route touching AWS, not one place worth try/excepting. Handled here so the
    console gets a code it can translate instead of a bare 500.

    Every other `ClientError` is re-raised, which leaves it exactly where it was
    before this handler existed: an unhandled 500 with the AWS error in the log.
    """
    # Imported here: `core` must not take a startup dependency on `services`.
    from app.services.aws_clients import assume_role_diagnostic, is_assume_role_failure

    if not is_assume_role_failure(exc):
        raise exc
    return JSONResponse(
        status_code=502,
        content=envelope(
            "workspace.assume_role_failed",
            assume_role_diagnostic(exc),
            {"aws_error_code": str(exc.response.get("Error", {}).get("Code") or "")},
        ),
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(ClientError, assume_role_error_handler)
