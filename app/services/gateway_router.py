"""
Gateway Router
==============
Selects the best gateway using weighted scoring:

Score = 0.35×success_rate + 0.20×(1-latency) + 0.20×(1-cost) + 0.15×health + 0.10×fit
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.constants import (
    DEFAULT_WEIGHTS, GATEWAY_COSTS, GATEWAY_METHODS,
    GatewayName, PaymentMethod,
)
from app.models.models import GatewayHealthMetric, RoutingConfig
from app.services import circuit_breaker as cb_svc

logger = logging.getLogger(__name__)


@dataclass
class GatewayScore:
    gateway: GatewayName
    total_score: float = 0.0
    success_rate_score: float = 0.0
    latency_score: float = 0.0
    cost_score: float = 0.0
    health_score: float = 0.0
    fit_score: float = 0.0
    raw_success_rate: float = 0.0
    raw_p95_latency_ms: int = 400
    raw_cost_paise: int = 0
    circuit_state: str = "CLOSED"


@dataclass
class RouteDecision:
    selected: GatewayName
    scores: list[GatewayScore] = field(default_factory=list)
    reason: str = ""


async def _get_weights(db: AsyncSession) -> dict[str, float]:
    result = await db.execute(
        select(RoutingConfig).where(RoutingConfig.config_key.like("weight_%"))
    )
    rows = result.scalars().all()
    weights = dict(DEFAULT_WEIGHTS)
    for r in rows:
        key = r.config_key.replace("weight_", "")
        weights[key] = float(r.config_value)
    return weights


async def _get_metrics(db: AsyncSession, gateway: GatewayName) -> tuple[float, int]:
    """Returns (success_rate, p95_latency_ms) from last 15 minutes."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
    # Use text query to avoid SQLAlchemy Row attribute deprecation issues
    result = await db.execute(
        text("""
            SELECT
                COALESCE(SUM(success_count), 0) as total_success,
                COALESCE(SUM(total_requests), 0) as total_requests,
                COALESCE(AVG(p95_latency_ms), 400) as avg_latency
            FROM gateway_health_metrics
            WHERE gateway = :gateway AND window_start >= :cutoff
        """),
        {"gateway": gateway.value, "cutoff": cutoff},
    )
    row = result.fetchone()
    if not row or row[1] == 0:
        return 0.95, 400  # defaults when no data
    success = float(row[0])
    total = float(row[1])
    latency = int(row[2])
    return (success / total) if total > 0 else 0.95, latency


async def select_gateway(
    db: AsyncSession,
    payment_method: PaymentMethod,
    amount_paise: int,
    trace_id: str,
    transaction_id: str | None = None,
) -> RouteDecision:
    weights = await _get_weights(db)
    scores: list[GatewayScore] = []
    MIN_LAT, MAX_LAT = 100, 2000

    for gateway in GatewayName:
        health = await cb_svc.get_health_score(db, gateway)
        cb_allowed = await cb_svc.is_allowed(db, gateway)

        if not cb_allowed:
            continue

        if payment_method not in GATEWAY_METHODS.get(gateway, []):
            continue

        sr, p95 = await _get_metrics(db, gateway)

        norm_lat = max(0.0, min(1.0, (p95 - MIN_LAT) / (MAX_LAT - MIN_LAT)))
        latency_score = 1.0 - norm_lat

        pct, fixed = GATEWAY_COSTS.get(gateway, (0.02, 200))
        cost_paise = int(amount_paise * pct) + fixed
        cost_score = 1.0 - min(1.0, cost_paise / 500)

        from app.models.models import CircuitBreakerState
        cb_result = await db.execute(
            select(CircuitBreakerState).where(CircuitBreakerState.gateway == gateway.value)
        )
        cb_row = cb_result.scalar_one_or_none()
        cb_state_label = cb_row.state if cb_row else "CLOSED"

        gs = GatewayScore(
            gateway=gateway,
            success_rate_score=sr,
            latency_score=latency_score,
            cost_score=cost_score,
            health_score=health,
            fit_score=1.0,
            raw_success_rate=sr,
            raw_p95_latency_ms=p95,
            raw_cost_paise=cost_paise,
            circuit_state=cb_state_label,
        )
        gs.total_score = (
            weights["success_rate"] * gs.success_rate_score
            + weights["latency"]      * gs.latency_score
            + weights["cost"]         * gs.cost_score
            + weights["health"]       * gs.health_score
            + weights["fit"]          * gs.fit_score
        )
        scores.append(gs)

    if not scores:
        raise ValueError("ALL_GATEWAYS_UNAVAILABLE: No gateway available for this payment method")

    scores.sort(key=lambda s: s.total_score, reverse=True)
    selected = scores[0]

    if selected.circuit_state == "HALF_OPEN" and len(scores) > 1:
        second = scores[1]
        if (selected.total_score - second.total_score) < 0.20:
            selected = second

    logger.info(
        "Route selected: %s score=%.4f trace=%s",
        selected.gateway.value, selected.total_score, trace_id,
    )

    reason = (
        f"Score:{selected.total_score:.4f} "
        f"sr={selected.success_rate_score:.2f} lat={selected.latency_score:.2f} "
        f"cost={selected.cost_score:.2f} health={selected.health_score:.2f}"
    )
    return RouteDecision(selected=selected.gateway, scores=scores, reason=reason)


async def get_failover_gateway(
    db: AsyncSession,
    payment_method: PaymentMethod,
    amount_paise: int,
    trace_id: str,
    exclude: list[GatewayName],
) -> GatewayName | None:
    try:
        decision = await select_gateway(db, payment_method, amount_paise, trace_id)
        for score in decision.scores:
            if score.gateway not in exclude:
                logger.info("Failover to %s", score.gateway.value)
                return score.gateway
    except ValueError:
        pass
    return None


async def record_metric(
    db: AsyncSession,
    gateway: GatewayName,
    success: bool,
    latency_ms: int,
) -> None:
    now = datetime.now(timezone.utc)
    window = now.replace(second=0, microsecond=0)
    metric = GatewayHealthMetric(
        gateway=gateway.value,
        window_start=window,
        total_requests=1,
        success_count=1 if success else 0,
        failure_count=0 if success else 1,
        p95_latency_ms=latency_ms,
    )
    db.add(metric)
    await db.flush()


async def get_weights(db: AsyncSession) -> dict[str, float]:
    return await _get_weights(db)


async def update_weights(db: AsyncSession, new_weights: dict[str, float]) -> None:
    total = sum(new_weights.values())
    if abs(total - 1.0) > 0.001:
        raise ValueError(f"Routing weights must sum to 1.0, got {total:.3f}")
    for key, value in new_weights.items():
        result = await db.execute(
            select(RoutingConfig).where(RoutingConfig.config_key == f"weight_{key}")
        )
        row = result.scalar_one_or_none()
        if row:
            row.config_value = value
        else:
            db.add(RoutingConfig(config_key=f"weight_{key}", config_value=value))
    await db.flush()