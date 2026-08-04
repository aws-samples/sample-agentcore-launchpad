"""Error-code envelope.

The backend returns machine-readable error codes; the frontend translates
them into the active locale. Envelope shape: {code, message, detail}.
"""

from typing import Any

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


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(  # nosemgrep: useless-inner-function
        _: Request, exc: AppError
    ) -> JSONResponse:
        # FastAPI registers this local callback through the decorator.
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope(exc.code, exc.message, exc.detail),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(  # nosemgrep: useless-inner-function
        _: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # FastAPI registers this local callback through the decorator.
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope(f"http.{exc.status_code}", str(exc.detail), None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(  # nosemgrep: useless-inner-function
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # FastAPI registers this local callback through the decorator.
        return JSONResponse(
            status_code=422,
            content=envelope(
                "validation.invalid_request",
                "Request validation failed",
                # ctx of custom validators may carry exception objects
                jsonable_encoder(exc.errors(), custom_encoder={Exception: str}),
            ),
        )
