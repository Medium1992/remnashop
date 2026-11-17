"""Errors the cabinet understands: ``{"detail", "code"}``, translated by code."""

from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse


class CabinetError(Exception):
    def __init__(self, status: int, code: str, detail: str = "", **extra: object) -> None:
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail or code
        self.extra = extra


def not_found(code: str = "SUBSCRIPTION_NOT_FOUND", detail: str = "Not found") -> CabinetError:
    return CabinetError(404, code, detail)


def unauthorized(detail: str = "Not authenticated", code: Optional[str] = None) -> CabinetError:
    return CabinetError(401, code or "UNAUTHORIZED", detail)


async def handle_cabinet_error(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, CabinetError)
    body: dict[str, object] = {"detail": error.detail, "code": error.code, **error.extra}
    return JSONResponse(status_code=error.status, content=body)
