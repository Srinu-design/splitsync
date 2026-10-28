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


def make_problem_response(
    status: int, code: str, title: str, detail: str, *, headers: dict | None = None, **extra
) -> JSONResponse:
    content = {
        "type": f"/errors/{code.lower().replace('_', '-')}",
        "title": title,
        "status": status,
        "code": code,
        "detail": detail,
        **extra,
    }
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        headers=headers,
        content=content,
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        request.state.error_code = exc.code
        return make_problem_response(
            exc.status, exc.code, exc.title, exc.detail, headers=exc.headers, **exc.extra
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        request.state.error_code = "VALIDATION_ERROR"
        # DECISION: A fixed validation detail avoids reflecting untrusted input or error contexts.
        return make_problem_response(
            422,
            "VALIDATION_ERROR",
            "Invalid request",
            "The request body or query failed validation.",
        )
