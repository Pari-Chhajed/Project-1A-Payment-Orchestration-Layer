"""
Circuit Breaker Pattern
=======================
Three states: CLOSED (normal) → OPEN (failing) → HALF_OPEN (testing)

CLOSED:    All requests pass through. Tracks consecutive failures.
OPEN:      All requests rejected immediately (fail-fast). Waits for timeout.
HALF_OPEN: Allows one test request. Success → CLOSED. Failure → OPEN.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.constants import CircuitState, GatewayName
from app.config.settings import settings
from app.models.models import CircuitBreakerState

logger = logging.getLogger(__name__)


async def _get_or_create(db: AsyncSession, gateway: str, payment_method: str = "all") -> CircuitBreakerState:
    result = await db.execute(
        select(CircuitBreakerState).where(
            CircuitBreakerState.gateway == gateway,
            CircuitBreakerState.payment_method == payment_method,
        ).with_for_update()
    )
    cb = result.scalar_one_or_none()
    if not cb:
        cb = CircuitBreakerState(
            gateway=gateway,
            payment_method=payment_method,
            state=CircuitState.CLOSED.value,
            failure_threshold=settings.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
            timeout_seconds=settings.CIRCUIT_BREAKER_TIMEOUT_SECONDS,
        )
        db.add(cb)
        await db.flush()
    return cb


async def is_allowed(db: AsyncSession, gateway: GatewayName, payment_method: str = "all") -> bool:
    cb = await _get_or_create(db, gateway.value, payment_method)

    if cb.state == CircuitState.CLOSED.value:
        return True

    if cb.state == CircuitState.OPEN.value:
        if cb.opened_at:
            elapsed = (datetime.now(timezone.utc) - cb.opened_at.replace(tzinfo=timezone.utc)).total_seconds()
            if elapsed >= cb.timeout_seconds:
                cb.state = CircuitState.HALF_OPEN.value
                cb.success_count = 0
                cb.updated_at = datetime.now(timezone.utc)
                await db.flush()
                logger.info("Circuit breaker %s → HALF_OPEN", gateway.value)
                return True
        logger.debug("Circuit breaker OPEN for %s — request rejected", gateway.value)
        return False

    if cb.state == CircuitState.HALF_OPEN.value:
        return cb.success_count < 1  # allow one test call

    return False


async def record_success(db: AsyncSession, gateway: GatewayName, payment_method: str = "all") -> None:
    cb = await _get_or_create(db, gateway.value, payment_method)
    if cb.state == CircuitState.HALF_OPEN.value:
        cb.state = CircuitState.CLOSED.value
        cb.failure_count = 0
        cb.success_count = 0
        cb.opened_at = None
        logger.info("Circuit breaker %s → CLOSED (test succeeded)", gateway.value)
    else:
        cb.failure_count = 0
    cb.last_success_at = datetime.now(timezone.utc)
    cb.updated_at = datetime.now(timezone.utc)
    await db.flush()


async def record_failure(db: AsyncSession, gateway: GatewayName, payment_method: str = "all") -> None:
    cb = await _get_or_create(db, gateway.value, payment_method)
    cb.failure_count += 1
    cb.last_failure_at = datetime.now(timezone.utc)
    cb.updated_at = datetime.now(timezone.utc)

    if cb.state == CircuitState.HALF_OPEN.value:
        cb.state = CircuitState.OPEN.value
        cb.opened_at = datetime.now(timezone.utc)
        logger.warning("Circuit breaker %s → OPEN (test failed)", gateway.value)
    elif cb.failure_count >= cb.failure_threshold:
        cb.state = CircuitState.OPEN.value
        cb.opened_at = datetime.now(timezone.utc)
        logger.error("Circuit breaker %s TRIPPED → OPEN (failures=%d)", gateway.value, cb.failure_count)

    await db.flush()


async def get_health_score(db: AsyncSession, gateway: GatewayName, payment_method: str = "all") -> float:
    """1.0=healthy, 0.5=degraded(HALF_OPEN), 0.0=down(OPEN)"""
    allowed = await is_allowed(db, gateway, payment_method)
    if not allowed:
        return 0.0
    result = await db.execute(
        select(CircuitBreakerState).where(
            CircuitBreakerState.gateway == gateway.value,
            CircuitBreakerState.payment_method == payment_method,
        )
    )
    cb = result.scalar_one_or_none()
    if cb and cb.state == CircuitState.HALF_OPEN.value:
        return 0.5
    return 1.0


async def get_all_states(db: AsyncSession) -> list[dict]:
    result = await db.execute(select(CircuitBreakerState))
    rows = result.scalars().all()
    return [
        {
            "gateway": r.gateway,
            "state": r.state,
            "failure_count": r.failure_count,
            "last_failure_at": r.last_failure_at.isoformat() if r.last_failure_at else None,
            "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None,
        }
        for r in rows
    ]


async def reset(db: AsyncSession, gateway: GatewayName) -> None:
    cb = await _get_or_create(db, gateway.value)
    cb.state = CircuitState.CLOSED.value
    cb.failure_count = 0
    cb.success_count = 0
    cb.opened_at = None
    cb.updated_at = datetime.now(timezone.utc)
    await db.flush()
    logger.info("Circuit breaker %s manually reset → CLOSED", gateway.value)
