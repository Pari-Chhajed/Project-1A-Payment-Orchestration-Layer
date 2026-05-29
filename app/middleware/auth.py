from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from app.config.settings import settings
from app.observability import correlation_id
import hashlib
import hmac
import logging, time, uuid

logger = logging.getLogger(__name__)


async def api_key_middleware(request: Request, call_next):
    # Skip auth for health and webhook endpoints
    if request.url.path in ("/api/v1/health", "/api/v1/live", "/api/v1/ready", "/metrics", "/") or request.url.path.startswith("/api/v1/webhooks"):
        return await call_next(request)

    key = request.headers.get("X-API-Key") or request.headers.get("x-api-key")
    valid = False
    if key and settings.API_KEY_HASH:
        valid = hmac.compare_digest(hashlib.sha256(key.encode()).hexdigest(), settings.API_KEY_HASH)
    elif key:
        valid = hmac.compare_digest(key, settings.API_KEY)
    if not valid:
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "UNAUTHORIZED", "message": "Invalid or missing API key. Provide X-API-Key header."}},
        )
    return await call_next(request)


async def request_logger_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    correlation_id.set(request_id)
    start = time.time()
    response = await call_next(request)
    duration_ms = int((time.time() - start) * 1000)
    logger.info("%s %s %d %dms id=%s", request.method, request.url.path, response.status_code, duration_ms, request_id)
    response.headers["X-Request-ID"] = request_id
    return response


def make_error_response(code: str, message: str, status: int, details: dict = None, request_id: str = None) -> JSONResponse:
    import datetime
    body = {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        }
    }
    if request_id:
        body["error"]["request_id"] = request_id
    return JSONResponse(status_code=status, content=body)
