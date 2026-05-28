from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from app.config.settings import settings
import logging, time, uuid

logger = logging.getLogger(__name__)


async def api_key_middleware(request: Request, call_next):
    # Skip auth for health and webhook endpoints
    EXCLUDED_PATHS = {
        "/",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/favicon.ico",
        "/api/v1/health",
    }

    if (
        request.url.path in EXCLUDED_PATHS
        or request.url.path.startswith("/api/v1/webhooks")
    ):
        return await call_next(request)
    
    key = request.headers.get("X-API-Key") or request.headers.get("x-api-key")
    if not key or key != settings.API_KEY:
        return JSONResponse(
            status_code=401,
            content={"error": {"code": "UNAUTHORIZED", "message": "Invalid or missing API key. Provide X-API-Key header."}},
        )
    return await call_next(request)


async def request_logger_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
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