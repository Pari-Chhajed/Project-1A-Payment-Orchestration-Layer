"""PayFlow Commerce FastAPI application."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config.database import close_db, init_db
from app.config.settings import settings
from app.middleware.auth import api_key_middleware, request_logger_middleware
from app.middleware.security import (
    rate_limit_middleware,
    request_size_middleware,
    secure_headers_middleware,
)
from app.observability import configure_logging, metrics_endpoint, metrics_middleware
from app.routes.other import (
    analytics_router,
    gateways_router,
    health_router,
    reconciliation_router,
    routing_router,
)
from app.routes.payments import router as payments_router
from app.routes.webhooks import router as webhooks_router
from app.workers.scheduler import start_scheduler, stop_scheduler
from app.services.redis_client import close_redis
from scripts.seed_data import seed_database

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("PayFlow Commerce starting up")
    await init_db()
    if settings.SEED_ON_STARTUP:
        await seed_database()
        logger.info("Database seeded")
    if settings.START_SCHEDULER_IN_API:
        start_scheduler()
        logger.info("In-process scheduler started")
    yield
    if settings.START_SCHEDULER_IN_API:
        stop_scheduler()
    await close_redis()
    await close_db()
    logger.info("PayFlow Commerce shut down")


app = FastAPI(
    title="PayFlow Commerce - Payment Orchestration Layer",
    description=(
        "Payment orchestration backend with multi-gateway routing, failover, "
        "idempotency, durable webhooks, reconciliation, and audit trails."
    ),
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url="/redoc" if settings.ENVIRONMENT != "production" else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ALLOWED_ORIGINS.split(",") if o.strip()],
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "X-API-Key",
        "X-Request-ID",
        "X-Mock-Response",
        "X-Mock-Delay-Ms",
        "X-Mock-Gateway",
        "X-Razorpay-Signature",
        "Stripe-Signature",
        "X-UPI-Signature",
    ],
)


@app.middleware("http")
async def secure_headers(request: Request, call_next):
    return await secure_headers_middleware(request, call_next)


@app.middleware("http")
async def enforce_request_size(request: Request, call_next):
    return await request_size_middleware(request, call_next)


@app.middleware("http")
async def rate_limit_requests(request: Request, call_next):
    return await rate_limit_middleware(request, call_next)


@app.middleware("http")
async def collect_metrics(request: Request, call_next):
    return await metrics_middleware(request, call_next)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    return await request_logger_middleware(request, call_next)


@app.middleware("http")
async def check_api_key(request: Request, call_next):
    return await api_key_middleware(request, call_next)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception")
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred"}},
    )


app.include_router(payments_router)
app.include_router(webhooks_router)
app.include_router(gateways_router)
app.include_router(routing_router)
app.include_router(reconciliation_router)
app.include_router(analytics_router)
app.include_router(health_router)
app.add_api_route("/metrics", metrics_endpoint, methods=["GET"], include_in_schema=False)
