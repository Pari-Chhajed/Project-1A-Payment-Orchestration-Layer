"""
PayFlow Commerce — Payment Orchestration Layer
FastAPI Application Entry Point
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config.database import init_db
from app.middleware.auth import api_key_middleware, request_logger_middleware
from app.routes.payments import router as payments_router
from app.routes.webhooks import router as webhooks_router
from app.routes.other import (
    gateways_router, routing_router, reconciliation_router,
    analytics_router, health_router,
)
from app.workers.scheduler import start_scheduler, stop_scheduler
from scripts.seed_data import seed_database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────
    logger.info("PayFlow Commerce starting up...")
    await init_db()
    logger.info("Database initialized")
    await seed_database()
    logger.info("Database seeded")
    start_scheduler()
    logger.info("Background workers started")
    yield
    # ── Shutdown ─────────────────────────────────────────────────
    stop_scheduler()
    logger.info("PayFlow Commerce shut down")


app = FastAPI(
    title="PayFlow Commerce — Payment Orchestration Layer",
    description=(
        "Production-grade payment backend with multi-gateway failover, "
        "idempotency, webhook deduplication, and automated reconciliation."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── Middleware ────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    return await request_logger_middleware(request, call_next)


@app.middleware("http")
async def check_api_key(request: Request, call_next):
    return await api_key_middleware(request, call_next)


# ── Global exception handler ─────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred"}},
    )


# ── Routes ────────────────────────────────────────────────────────
app.include_router(payments_router)
app.include_router(webhooks_router)
app.include_router(gateways_router)
app.include_router(routing_router)
app.include_router(reconciliation_router)
app.include_router(analytics_router)
app.include_router(health_router)