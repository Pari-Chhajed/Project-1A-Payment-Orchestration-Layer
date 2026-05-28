"""All non-payment routes: gateways, routing, reconciliation, analytics, health."""
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.constants import GatewayName
from app.config.database import get_db
from app.middleware.auth import make_error_response
from app.models.models import (
    GatewayHealthMetric, ReconciliationRun, RoutingConfig, Transaction,
)
from app.services import circuit_breaker as cb_svc
from app.services import gateway_router
from app.services import reconciliation_service as recon_svc

# ─────────────────────────────────────────────────────────────────
# GATEWAYS
# ─────────────────────────────────────────────────────────────────
gateways_router = APIRouter(prefix="/api/v1/gateways", tags=["Gateways"])


@gateways_router.get("")
async def list_gateways(db: AsyncSession = Depends(get_db)):
    states = await cb_svc.get_all_states(db)
    return {"data": states}


@gateways_router.get("/{name}/health")
async def gateway_health(name: str, db: AsyncSession = Depends(get_db)):
    try:
        gw = GatewayName(name)
    except ValueError:
        return make_error_response("NOT_FOUND", f"Gateway {name} not found", 404)

    health_score = await cb_svc.get_health_score(db, gw)
    cb_state = await cb_svc._get_or_create(db, name)

    # Last 5 min metrics
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    result = await db.execute(
        select(
            func.sum(GatewayHealthMetric.success_count),
            func.sum(GatewayHealthMetric.total_requests),
            func.avg(GatewayHealthMetric.p95_latency_ms),
        ).where(
            GatewayHealthMetric.gateway == name,
            GatewayHealthMetric.window_start >= cutoff,
        )
    )
    row = result.one()
    success = int(row[0] or 0)
    total = int(row[1] or 0)
    avg_lat = int(row[2] or 0)
    sr = round(success / total * 100, 2) if total else 0

    return {
        "data": {
            "gateway": name,
            "circuit_state": cb_state.state,
            "health_score": health_score,
            "failure_count": cb_state.failure_count,
            "success_rate_pct": sr,
            "avg_p95_latency_ms": avg_lat,
            "last_failure_at": cb_state.last_failure_at.isoformat() if cb_state.last_failure_at else None,
            "last_success_at": cb_state.last_success_at.isoformat() if cb_state.last_success_at else None,
        }
    }


@gateways_router.get("/{name}/metrics")
async def gateway_metrics(name: str, db: AsyncSession = Depends(get_db)):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    result = await db.execute(
        select(GatewayHealthMetric)
        .where(
            GatewayHealthMetric.gateway == name,
            GatewayHealthMetric.window_start >= cutoff,
        )
        .order_by(GatewayHealthMetric.window_start.desc())
        .limit(60)
    )
    rows = result.scalars().all()
    return {
        "data": [
            {
                "gateway": r.gateway,
                "window_start": r.window_start.isoformat(),
                "total_requests": r.total_requests,
                "success_count": r.success_count,
                "failure_count": r.failure_count,
                "p95_latency_ms": r.p95_latency_ms,
            }
            for r in rows
        ]
    }


class GatewayConfigUpdate(BaseModel):
    failure_threshold: Optional[int] = None
    timeout_seconds: Optional[int] = None


@gateways_router.put("/{name}/config")
async def update_gateway_config(name: str, body: GatewayConfigUpdate, db: AsyncSession = Depends(get_db)):
    try:
        gw = GatewayName(name)
    except ValueError:
        return make_error_response("NOT_FOUND", f"Gateway {name} not found", 404)
    cb = await cb_svc._get_or_create(db, name)
    if body.failure_threshold is not None:
        cb.failure_threshold = body.failure_threshold
    if body.timeout_seconds is not None:
        cb.timeout_seconds = body.timeout_seconds
    await db.commit()
    return {"message": f"Gateway {name} config updated"}


@gateways_router.post("/{name}/circuit-breaker/reset")
async def reset_circuit_breaker(name: str, db: AsyncSession = Depends(get_db)):
    try:
        gw = GatewayName(name)
    except ValueError:
        return make_error_response("NOT_FOUND", f"Gateway {name} not found", 404)
    await cb_svc.reset(db, gw)
    await db.commit()
    return {"message": f"Circuit breaker for {name} reset to CLOSED"}


# ─────────────────────────────────────────────────────────────────
# ROUTING
# ─────────────────────────────────────────────────────────────────
routing_router = APIRouter(prefix="/api/v1/routing", tags=["Routing"])


@routing_router.get("/config")
async def get_routing_config(db: AsyncSession = Depends(get_db)):
    weights = await gateway_router.get_weights(db)
    return {"data": weights}


class RoutingWeightsUpdate(BaseModel):
    success_rate: float
    latency: float
    cost: float
    health: float
    fit: float


@routing_router.put("/config")
async def update_routing_config(body: RoutingWeightsUpdate, db: AsyncSession = Depends(get_db)):
    try:
        await gateway_router.update_weights(db, body.model_dump())
        await db.commit()
        return {"message": "Routing weights updated"}
    except ValueError as e:
        return make_error_response("VALIDATION_ERROR", str(e), 400)


# ─────────────────────────────────────────────────────────────────
# RECONCILIATION
# ─────────────────────────────────────────────────────────────────
reconciliation_router = APIRouter(prefix="/api/v1/reconciliation", tags=["Reconciliation"])


@reconciliation_router.post("/trigger")
async def trigger_reconciliation(db: AsyncSession = Depends(get_db)):
    try:
        report = await recon_svc.run(db, triggered_by="manual")
        return {"data": report}
    except Exception as e:
        return make_error_response("RECONCILIATION_ERROR", str(e), 500)


@reconciliation_router.get("/reports/{run_id}")
async def get_reconciliation_report(run_id: str, db: AsyncSession = Depends(get_db)):
    try:
        report = await recon_svc.get_report(db, run_id)
        return {"data": report}
    except ValueError as e:
        return make_error_response("NOT_FOUND", str(e), 404)


@reconciliation_router.get("/reports")
async def list_reconciliation_reports(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ReconciliationRun).order_by(ReconciliationRun.started_at.desc()).limit(20)
    )
    runs = result.scalars().all()
    return {
        "data": [
            {
                "id": r.id,
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "transactions_checked": r.transactions_checked,
                "discrepancies_found": r.discrepancies_found,
                "anomalies_found": r.anomalies_found,
            }
            for r in runs
        ]
    }


# ─────────────────────────────────────────────────────────────────
# ANALYTICS
# ─────────────────────────────────────────────────────────────────
analytics_router = APIRouter(prefix="/api/v1/analytics", tags=["Analytics"])


@analytics_router.get("/success-rate")
async def success_rate(hours: int = 24, db: AsyncSession = Depends(get_db)):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = await db.execute(
        select(
            GatewayHealthMetric.gateway,
            func.sum(GatewayHealthMetric.total_requests).label("total"),
            func.sum(GatewayHealthMetric.success_count).label("success"),
            func.avg(GatewayHealthMetric.p95_latency_ms).label("avg_lat"),
        )
        .where(GatewayHealthMetric.window_start >= cutoff)
        .group_by(GatewayHealthMetric.gateway)
        .order_by(GatewayHealthMetric.gateway)
    )
    rows = result.all()
    return {
        "data": [
            {
                "gateway": r.gateway,
                "total_requests": int(r.total or 0),
                "success_count": int(r.success or 0),
                "success_rate_pct": round(int(r.success or 0) / int(r.total or 1) * 100, 2),
                "avg_p95_latency_ms": int(r.avg_lat or 0),
            }
            for r in rows
        ],
        "period_hours": hours,
    }


@analytics_router.get("/volume")
async def volume(hours: int = 24, db: AsyncSession = Depends(get_db)):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = await db.execute(
        select(Transaction)
        .where(Transaction.created_at >= cutoff)
        .order_by(Transaction.created_at.desc())
        .limit(500)
    )
    txns = result.scalars().all()
    total = len(txns)
    captured = sum(1 for t in txns if t.state == "CAPTURED")
    failed = sum(1 for t in txns if t.state == "FAILED")
    volume_paise = sum(t.amount_paise for t in txns if t.state == "CAPTURED")
    from app.utils.money import paise_to_rupees
    return {
        "data": {
            "total_transactions": total,
            "captured": captured,
            "failed": failed,
            "success_rate_pct": round(captured / total * 100, 2) if total else 0,
            "captured_volume": paise_to_rupees(volume_paise),
            "captured_volume_paise": volume_paise,
        },
        "period_hours": hours,
    }


# ─────────────────────────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────────────────────────
health_router = APIRouter(tags=["Health"])


@health_router.get("/api/v1/health")
async def health(db: AsyncSession = Depends(get_db)):
    import time
    try:
        await db.execute(select(func.now()))
        db_status = "connected"
    except Exception:
        db_status = "error"

    cb_states = await cb_svc.get_all_states(db)
    gw_health = {s["gateway"]: ("healthy" if s["state"] == "CLOSED" else
                                 "degraded" if s["state"] == "HALF_OPEN" else "down")
                  for s in cb_states}
    overall = "healthy" if all(v != "down" for v in gw_health.values()) and db_status == "connected" else "degraded"

    return {
        "status": overall,
        "database": db_status,
        "gateways": gw_health,
        "uptime_seconds": int(time.time()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@health_router.get("/")
async def root():
    return {"message": "PayFlow Commerce API", "docs": "/docs", "health": "/api/v1/health"}