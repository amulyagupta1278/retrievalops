from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse


@dataclass(frozen=True, slots=True)
class ServiceError(Exception):
    status_code: int
    code: str
    message: str


async def service_error_handler(_request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, ServiceError):
        raise error
    return JSONResponse(
        status_code=error.status_code,
        content={"error": {"code": error.code, "message": error.message}},
    )
