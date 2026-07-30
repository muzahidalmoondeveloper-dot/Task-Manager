from datetime import UTC, datetime

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.auth_errors import AppException


def _build_error_response(
    code: str,
    message: str,
    request_id: str | None = None,
    details: dict | None = None,
    fields: dict | None = None,
) -> dict:
    error_dict: dict = {
        "code": code,
        "message": message,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if request_id:
        error_dict["request_id"] = request_id
    if details:
        error_dict["details"] = details
    if fields:
        error_dict["fields"] = fields
    return {"error": error_dict}


def _status_to_code(status_code: int) -> str:
    mapping = {
        400: "BAD_REQUEST",
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        409: "CONFLICT",
        422: "VALIDATION_FAILED",
        429: "RATE_LIMITED",
        500: "INTERNAL_ERROR",
    }
    return mapping.get(status_code, f"HTTP_{status_code}")


def _get_request_id(request: Request) -> str | None:
    return request.headers.get("X-Request-Id")


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_dict(request_id=_get_request_id(request)),
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    request_id = _get_request_id(request)
    code = _status_to_code(exc.status_code)

    if isinstance(exc.detail, dict):
        message = exc.detail.get("message", str(exc.detail))
        details = {k: v for k, v in exc.detail.items() if k != "message"} or None
    else:
        message = str(exc.detail) if exc.detail else "An error occurred."
        details = None

    return JSONResponse(
        status_code=exc.status_code,
        content=_build_error_response(code=code, message=message, request_id=request_id, details=details),
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    request_id = _get_request_id(request)
    fields: dict[str, list[str]] = {}

    for error in exc.errors():
        loc = error.get("loc", [])
        field = ".".join(str(p) for p in loc[1:]) if len(loc) > 1 else (str(loc[0]) if loc else "unknown")
        fields.setdefault(field, []).append(error.get("msg", "Invalid value"))

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_build_error_response(
            code="VALIDATION_FAILED",
            message="Validation failed.",
            request_id=request_id,
            fields=fields,
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppException, app_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
