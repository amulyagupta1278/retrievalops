import hashlib
import re
import threading
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Final

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

_SANDBOX_PATH: Final = re.compile(r"^/v1/sandboxes/([0-9a-fA-F-]{36})(?:/|$)")
_SECURITY_HEADERS: Final = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'; script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:"
    ),
    "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class SandboxRateLimitMiddleware(BaseHTTPMiddleware):
    """Bound protected requests per sandbox without retaining credentials or content."""

    def __init__(self, app: Callable[..., Awaitable[None]], *, limit: int) -> None:
        super().__init__(app)
        self._limit = limit
        self._window_seconds = 60.0
        self._requests: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        match = _SANDBOX_PATH.match(request.url.path)
        token = request.headers.get("X-Sandbox-Token")
        key = hashlib.sha256(token.encode()).hexdigest() if token else None
        if key is None and match is not None:
            key = match.group(1).lower()
        if key is not None and not self._allow(key, time.monotonic()):
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": "60"},
                content={
                    "error": {
                        "code": "SANDBOX_RATE_LIMITED",
                        "message": "The sandbox request limit was reached. Retry shortly.",
                    }
                },
            )
        return await call_next(request)

    def _allow(self, key: str, now: float) -> bool:
        threshold = now - self._window_seconds
        with self._lock:
            timestamps = self._requests[key]
            while timestamps and timestamps[0] <= threshold:
                timestamps.popleft()
            if len(timestamps) >= self._limit:
                return False
            timestamps.append(now)
            if len(self._requests) > 10_000:
                self._drop_idle(threshold)
            return True

    def _drop_idle(self, threshold: float) -> None:
        idle = [
            key for key, values in self._requests.items() if not values or values[-1] <= threshold
        ]
        for key in idle:
            del self._requests[key]


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers[name] = value
        return response
