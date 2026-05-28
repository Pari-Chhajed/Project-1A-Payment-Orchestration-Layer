"""Seed initial data: routing weights, circuit breaker states, historical metrics."""
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import AsyncSessionLocal
from app.config.constants import DEFAULT_WEIGHTS, GATEWAY_METHODS, GatewayName
from app.config.settings import settings
from app.models.models import CircuitBreakerState, GatewayHealthMetric, GatewayRateLimitState, RoutingConfig

logger = logging.getLogger(__name__)

# Historical data from Section A3.4 of the PDF
HISTORICAL_DATA = [
    # (gateway, hour_of_day, success_rate, p95_latency_ms, transactions)
    ("razorpay", 0,  0.985, 320,  4200),
    ("razorpay", 6,  0.972, 450,  18500),
    ("razorpay", 12, 0.941, 780,  32000),
    ("razorpay", 18, 0.968, 520,  25300),
    ("stripe",   0,  0.991, 280,  3800),
    ("stripe",   6,  0.988, 310,  15200),
    ("stripe",   12, 0.975, 420,  28500),
    ("stripe",   18, 0.982, 350,  22100),
    ("payu",     0,  0.960, 400,  2100),
    ("payu",     6,  0.935, 620,  9800),
    ("payu",     12, 0.892, 950,  15500),
    ("payu",     18, 0.918, 750,  12200),
    ("upi",      0,  0.995, 180,  5600),
    ("upi",      6,  0.992, 210,  22000),
    ("upi",      12, 0.980, 350,  38000),
    ("upi",      18, 0.988, 250,  30500),
]


async def seed_database():
    async with AsyncSessionLocal() as db:
        try:
            await _seed_routing_config(db)
            await _seed_circuit_breakers(db)
            await _seed_rate_limits(db)
            await _seed_historical_metrics(db)
            await db.commit()
            logger.info("Database seeded successfully")
        except Exception as e:
            logger.warning("Seed skipped or partial: %s", e)


async def _seed_routing_config(db: AsyncSession):
    for key, value in DEFAULT_WEIGHTS.items():
        existing = await db.execute(
            select(RoutingConfig).where(RoutingConfig.config_key == f"weight_{key}")
        )
        if not existing.scalar_one_or_none():
            db.add(RoutingConfig(config_key=f"weight_{key}", config_value=value,
                                  description=f"Weight for {key} factor"))
    await db.flush()


async def _seed_circuit_breakers(db: AsyncSession):
    for gw in GatewayName:
        methods = {method.value for method in GATEWAY_METHODS[gw]} | {"all"}
        for method in methods:
            existing = await db.execute(
                select(CircuitBreakerState).where(
                    CircuitBreakerState.gateway == gw.value,
                    CircuitBreakerState.payment_method == method,
                )
            )
            if not existing.scalar_one_or_none():
                db.add(CircuitBreakerState(
                    gateway=gw.value,
                    payment_method=method,
                    state="CLOSED",
                    failure_threshold=5,
                    timeout_seconds=30,
                ))
    await db.flush()


async def _seed_rate_limits(db: AsyncSession):
    for gw in GatewayName:
        existing = await db.execute(
            select(GatewayRateLimitState).where(GatewayRateLimitState.gateway == gw.value)
        )
        if not existing.scalar_one_or_none():
            capacity = settings.GATEWAY_RATE_LIMIT_PER_SECOND
            db.add(GatewayRateLimitState(
                gateway=gw.value,
                capacity=capacity,
                refill_per_second=capacity,
                available_tokens=float(capacity),
            ))
    await db.flush()


async def _seed_historical_metrics(db: AsyncSession):
    now = datetime.now(timezone.utc)
    inserted = 0
    for gateway, hour, sr, p95, transactions in HISTORICAL_DATA:
        # Create entries for the last 3 days at this hour
        for day_offset in range(3):
            window = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            window -= timedelta(days=day_offset)
            success = int(transactions * sr)
            failure = transactions - success
            existing = await db.execute(
                select(GatewayHealthMetric).where(
                    GatewayHealthMetric.gateway == gateway,
                    GatewayHealthMetric.window_start == window,
                )
            )
            if not existing.scalar_one_or_none():
                db.add(GatewayHealthMetric(
                    gateway=gateway,
                    window_start=window,
                    total_requests=transactions,
                    success_count=success,
                    failure_count=failure,
                    p95_latency_ms=p95,
                ))
                inserted += 1
    if inserted:
        await db.flush()
        logger.info("Inserted %d historical metric records", inserted)
