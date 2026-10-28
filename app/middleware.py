import json
import re
import time
import uuid

from app.errors import make_problem_response
from app.logging import logger as access_logger

MAX_BODY_SIZE = 16 * 1024  # 16 KB = 16,384 bytes
REQUEST_ID_REGEX = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def extract_room_slug(path: str) -> str | None:
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 3 and parts[0] in ("api", "ws") and parts[1] == "rooms":
        return parts[2]
    if len(parts) >= 2 and parts[0] == "r":
        return parts[1]
    return None


def determine_outcome(status: int, headers: dict[str, str], error_code: str | None) -> str | None:
    # 1. Replays carry Idempotent-Replayed header
    if headers.get("idempotent-replayed", "").lower() == "true":
        return "REPLAYED"

    # 2. Created resource (201)
    if status == 201:
        return "CREATED"

    # 3. Rate limited (429)
    if status == 429:
        return "RATE_LIMITED"

    # 4. Conflict (409)
    if status == 409:
        if error_code in ("STALE_GRAPH_VERSION", "EXPENSE_CHANGED"):
            return "STALE"
        if error_code == "GROUP_BUSY":
            return "BUSY"
        return "REJECTED"

    # 5. Other 4xx / 5xx
    if 400 <= status < 600:
        return "REJECTED"

    # 6. Other 2xx
    if 200 <= status < 300:
        return None

    return None


class BodyLimitMiddleware:
    def __init__(self, app, max_body_size: int = MAX_BODY_SIZE):
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 1. Check Content-Length when present before reading body
        headers = dict(scope.get("headers", []))
        cl_header = headers.get(b"content-length")
        if cl_header:
            try:
                content_length = int(cl_header.decode("latin1"))
                if content_length > self.max_body_size:
                    scope.setdefault("state", {})["error_code"] = "PAYLOAD_TOO_LARGE"
                    # DECISION: SPEC gives no code for 413; use PAYLOAD_TOO_LARGE per instructions.
                    res = make_problem_response(
                        413,
                        "PAYLOAD_TOO_LARGE",
                        "Payload Too Large",
                        f"Request body exceeds {self.max_body_size // 1024} KB limit.",
                    )
                    await res(scope, receive, send)
                    return
            except ValueError:
                pass

        # 2. Buffer request body up to the limit before calling the app
        chunks = []
        total_bytes = 0
        while True:
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                total_bytes += len(chunk)
                if total_bytes > self.max_body_size:
                    scope.setdefault("state", {})["error_code"] = "PAYLOAD_TOO_LARGE"
                    # DECISION: Reject chunked body exceeding limit before calling the app.
                    res = make_problem_response(
                        413,
                        "PAYLOAD_TOO_LARGE",
                        "Payload Too Large",
                        f"Request body exceeds {self.max_body_size // 1024} KB limit.",
                    )
                    await res(scope, receive, send)
                    return
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                return

        body = b"".join(chunks)
        sent = False

        async def replayed_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, replayed_receive, send)


class ObservabilityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 1. Assign or validate X-Request-ID
        headers = dict(scope.get("headers", []))
        raw_req_id = headers.get(b"x-request-id")
        req_id_str = raw_req_id.decode("latin1").strip() if raw_req_id else ""
        if req_id_str and REQUEST_ID_REGEX.match(req_id_str):
            request_id = req_id_str
        else:
            request_id = uuid.uuid4().hex

        scope.setdefault("state", {})["request_id"] = request_id
        start_time = time.perf_counter()
        response_status = 500
        response_headers: dict[str, str] = {}

        async def send_wrapper(message):
            nonlocal response_status, response_headers
            if message["type"] == "http.response.start":
                response_status = message["status"]
                # Convert headers to string dict for outcome inspection
                raw_headers = list(message.get("headers", []))
                for k, v in raw_headers:
                    response_headers[k.decode("latin1").lower()] = v.decode("latin1")
                # Add X-Request-ID to response headers
                raw_headers.append((b"x-request-id", request_id.encode("latin1")))
                message["headers"] = raw_headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            # DECISION: Skip /healthz from JSON access log because Docker healthchecks are noise.
            if path != "/healthz":
                latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
                room_slug = extract_room_slug(path)
                error_code = scope.get("state", {}).get("error_code")
                outcome = determine_outcome(response_status, response_headers, error_code)
                log_data = {
                    "request_id": request_id,
                    "method": scope.get("method", ""),
                    "path": path,
                    "status": response_status,
                    "latency_ms": latency_ms,
                    "room_slug": room_slug,
                    "outcome": outcome,
                }
                access_logger.info(json.dumps(log_data))
