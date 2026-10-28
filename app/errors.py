from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status, code, title, detail, *, headers=None, **extra):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.extra = extra
        self.headers = headers


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status,
            media_type="application/problem+json",
            headers=exc.headers,
            content={
                "type": f"/errors/{exc.code.lower().replace('_', '-')}",
                "title": exc.title,
                "status": exc.status,
                "code": exc.code,
                "detail": exc.detail,
                **exc.extra,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        # DECISION: A fixed validation detail avoids reflecting untrusted input or error contexts.
        return await api_error_handler(
            request,
            ApiError(
                422,
                "VALIDATION_ERROR",
                "Invalid request",
                "The request body or query failed validation.",
            ),
        )
