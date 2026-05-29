import json
import logging
import sys
import time
from contextvars import ContextVar
from typing import Callable

from fastapi import Request, Response
from starlette.responses import PlainTextResponse

from app.config.settings import settings

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

try:
    from prometheus_client import Counter, Histogram, generate_latest
except Exception:  # pragma: no cover
    Counter = Histogram = None  # type: ignore


class _NoopMetric:
    def labels(self, *args, **kwargs):
        return self

    def inc(self, *args, **kwargs):
        return None

    def observe(self, *args, **kwargs):
        return None


if Counter and Histogram:
    REQUEST_COUNT = Counter("payflow_http_requests_total", "Total HTTP requests", ["method", "path", "status"])
    REQUEST_LATENCY = Histogram("payflow_http_request_duration_seconds", "HTTP request latency", ["method", "path"])
    PAYMENT_EVENTS = Counter("payflow_payment_events_total", "Payment domain events", ["event", "gateway"])
    QUEUE_EVENTS = Counter("payflow_queue_events_total", "Queue events", ["queue", "status"])
else:
    REQUEST_COUNT = REQUEST_LATENCY = PAYMENT_EVENTS = QUEUE_EVENTS = _NoopMetric()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "correlation_id": correlation_id.get(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.JSON_LOGS else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=settings.LOG_LEVEL, handlers=[handler], force=True)


async def metrics_middleware(request: Request, call_next: Callable) -> Response:
    start = time.perf_counter()
    path = request.scope.get("route").path if request.scope.get("route") else request.url.path
    try:
        response = await call_next(request)
    except Exception:
        REQUEST_COUNT.labels(request.method, path, "500").inc()
        raise
    elapsed = time.perf_counter() - start
    REQUEST_COUNT.labels(request.method, path, str(response.status_code)).inc()
    REQUEST_LATENCY.labels(request.method, path).observe(elapsed)
    return response


async def metrics_endpoint() -> PlainTextResponse:
    if "generate_latest" not in globals():
        return PlainTextResponse("", media_type="text/plain")
    return PlainTextResponse(generate_latest().decode("utf-8"), media_type="text/plain; version=0.0.4")
