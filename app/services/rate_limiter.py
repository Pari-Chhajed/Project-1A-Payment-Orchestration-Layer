import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.constants import GatewayName
from app.config.settings import settings
from app.models.models import GatewayRateLimitState


async def acquire(db: AsyncSession, gateway: GatewayName) -> float:
    result = await db.execute(
        select(GatewayRateLimitState)
        .where(GatewayRateLimitState.gateway == gateway.value)
        .with_for_update()
    )
    state = result.scalar_one_or_none()
    if not state:
        capacity = settings.GATEWAY_RATE_LIMIT_PER_SECOND
        state = GatewayRateLimitState(
            gateway=gateway.value,
            capacity=capacity,
            refill_per_second=capacity,
            available_tokens=float(capacity),
        )
        db.add(state)
        await db.flush()

    now = datetime.now(timezone.utc)
    last = state.updated_at.replace(tzinfo=timezone.utc) if state.updated_at else now
    elapsed = max(0.0, (now - last).total_seconds())
    state.available_tokens = min(
        float(state.capacity),
        float(state.available_tokens) + elapsed * state.refill_per_second,
    )

    wait_seconds = 0.0
    if state.available_tokens >= 1:
        state.available_tokens -= 1
    else:
        wait_seconds = (1 - state.available_tokens) / state.refill_per_second
        state.available_tokens = 0
    state.updated_at = now
    await db.flush()

    if wait_seconds > 0:
        await db.commit()
        await asyncio.sleep(wait_seconds)
    return wait_seconds


async def get_states(db: AsyncSession) -> list[dict]:
    result = await db.execute(select(GatewayRateLimitState).order_by(GatewayRateLimitState.gateway))
    rows = result.scalars().all()
    return [
        {
            "gateway": row.gateway,
            "capacity": row.capacity,
            "refill_per_second": row.refill_per_second,
            "available_tokens": round(row.available_tokens, 2),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in rows
    ]
