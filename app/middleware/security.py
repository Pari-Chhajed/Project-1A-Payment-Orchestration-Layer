import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config.settings import settings

_hits: dict[str, deque[float]] = defaultdict(deque)


async def request_size_middleware(request: Request, call_next):
    length = request.headers.get("content-length")
    if length and int(length) > settings.REQUEST_BODY_MAX_BYTES:
        return JSONResponse(
            status_code=413,
            content={"error": {"code": "REQUEST_TOO_LARGE", "message": "Request body exceeds configured limit"}},
        )
    return await call_next(request)


async def rate_limit_middleware(request: Request, call_next):
    if request.url.path in ("/api/v1/health", "/api/v1/live", "/metrics"):
        return await call_next(request)
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    window = _hits[ip]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= settings.RATE_LIMIT_PER_MINUTE:
        return JSONResponse(
            status_code=429,
            content={"error": {"code": "RATE_LIMITED", "message": "Too many requests"}},
        )
    window.append(now)
    return await call_next(request)


async def secure_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response
