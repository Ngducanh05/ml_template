from dataclasses import dataclass

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


@dataclass(frozen=True, slots=True)
class ApiError(Exception):
    status_code: int
    code: str
    message: str


async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message},
    )


async def validation_error_handler(
    _request: Request, _exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"code": "INVALID_REQUEST", "message": "Request payload is invalid"},
    )
