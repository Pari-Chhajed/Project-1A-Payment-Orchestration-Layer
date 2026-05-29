"""
Reconciliation Engine
=====================
Runs every 15 minutes (configurable) to find discrepancies
between internal DB state and gateway actual state.
"""
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.gateways import FETCH_FN
from app.config.constants import GatewayName, TransactionState
from app.config.settings import settings
from app.models.models import ReconciliationLog, ReconciliationRun, Transaction
from app.services import state_machine as sm

logger = logging.getLogger(__name__)

GATEWAY_STATUS_MAP = {
    "authorized": TransactionState.AUTHORISED,
    "captured":   TransactionState.CAPTURED,
    "succeeded":  TransactionState.CAPTURED,
    "success":    TransactionState.CAPTURED,
    "failed":     TransactionState.FAILED,
    "failure":    TransactionState.FAILED,
    "refunded":   TransactionState.REFUNDED,
    "pending":    TransactionState.AUTH_INITIATED,
    "expired":    TransactionState.AUTH_EXPIRED,
    "cancelled":  TransactionState.VOIDED,
}


async def run(db: AsyncSession, triggered_by: str = "scheduled") -> dict:
    started_at = datetime.now(timezone.utc)
    run_rec = ReconciliationRun(triggered_by=triggered_by)
    db.add(run_rec)
    await db.flush()
    run_id = run_rec.id
    logger.info("Reconciliation run %s started by %s", run_id, triggered_by)

    checked = discrepancies = anomalies = 0
    details = []

    try:
        stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.STALE_TRANSACTION_MINUTES)
        stale_states = [
            TransactionState.AUTH_INITIATED.value,
            TransactionState.CAPTURE_INITIATED.value,
            TransactionState.REFUND_INITIATED.value,
        ]
        result = await db.execute(
            select(Transaction).where(
                Transaction.state.in_(stale_states),
                Transaction.updated_at < stale_cutoff,
                Transaction.gateway.is_not(None),
                Transaction.gateway_reference.is_not(None),
            ).limit(500).with_for_update(skip_locked=True)
        )
        stale_txns = result.scalars().all()

        for txn in stale_txns:
            checked += 1
            detail = await _reconcile_one(db, txn, run_id)
            if detail:
                details.append(detail)
                discrepancies += 1
                if detail.get("requires_manual"):
                    anomalies += 1

        run_rec.status = "COMPLETED"
        run_rec.completed_at = datetime.now(timezone.utc)
        run_rec.transactions_checked = checked
        run_rec.discrepancies_found = discrepancies
        run_rec.anomalies_found = anomalies
        await db.commit()
        logger.info("Reconciliation %s done: checked=%d disc=%d anomalies=%d",
                    run_id, checked, discrepancies, anomalies)
    except Exception as e:
        run_rec.status = "FAILED"
        run_rec.error_message = str(e)
        await db.commit()
        raise

    return {
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "completed_at": run_rec.completed_at.isoformat() if run_rec.completed_at else None,
        "transactions_checked": checked,
        "discrepancies_found": discrepancies,
        "anomalies_found": anomalies,
        "details": details,
    }


async def _reconcile_one(db: AsyncSession, txn: Transaction, run_id: str) -> dict | None:
    try:
        fetch_fn = FETCH_FN.get(GatewayName(txn.gateway))
        if not fetch_fn:
            return None
        gw_status = await fetch_fn(txn.gateway_reference)
        mapped = GATEWAY_STATUS_MAP.get(gw_status.status)
        if not mapped:
            return None

        internal = TransactionState(txn.state)
        if internal == mapped:
            return None

        is_critical = (
            (internal == TransactionState.CAPTURED and mapped == TransactionState.FAILED)
            or (internal == TransactionState.REFUNDED and mapped == TransactionState.CAPTURED)
        )

        log = ReconciliationLog(
            run_id=run_id,
            transaction_id=txn.id,
            discrepancy_type="CRITICAL_ANOMALY" if is_critical else "STATE_MISMATCH",
            internal_state=internal.value,
            gateway_state=gw_status.status,
            resolution="MANUAL_REVIEW_REQUIRED" if is_critical else "AUTO_RECONCILED",
            requires_manual=is_critical,
            notes=(
                f"CRITICAL: internal={internal.value} gw={gw_status.status}"
                if is_critical
                else f"Auto-corrected {internal.value}→{mapped.value}"
            ),
        )
        db.add(log)
        await db.flush()

        if not is_critical:
            try:
                await sm.transition(
                    db, txn.id, mapped,
                    event="RECONCILIATION_OVERRIDE",
                    gateway_reference=txn.gateway_reference,
                    gateway_response=gw_status.raw_response,
                    meta={"run_id": run_id, "auto_reconciled": True},
                    created_by="reconciliation_engine",
                    trace_id=txn.trace_id,
                )
            except Exception:
                await sm.transition(
                    db, txn.id, TransactionState.RECONCILIATION_MISMATCH,
                    event="RECONCILIATION_MISMATCH",
                    meta={"run_id": run_id},
                    created_by="reconciliation_engine",
                    trace_id=txn.trace_id,
                )
        else:
            try:
                await sm.transition(
                    db, txn.id, TransactionState.RECONCILIATION_REQUIRED,
                    event="CRITICAL_RECONCILIATION_ANOMALY",
                    meta={"run_id": run_id, "requires_manual": True},
                    created_by="reconciliation_engine",
                    trace_id=txn.trace_id,
                )
            except Exception:
                pass

        return {
            "transaction_id": txn.id,
            "discrepancy_type": log.discrepancy_type,
            "internal_state": internal.value,
            "gateway_state": gw_status.status,
            "resolution": log.resolution,
            "requires_manual": is_critical,
        }
    except Exception as e:
        logger.error("Error reconciling txn %s: %s", txn.id, e)
        return None


async def get_report(db: AsyncSession, run_id: str) -> dict:
    run_res = await db.execute(select(ReconciliationRun).where(ReconciliationRun.id == run_id))
    run_rec = run_res.scalar_one_or_none()
    if not run_rec:
        raise ValueError(f"Reconciliation run {run_id} not found")

    log_res = await db.execute(
        select(ReconciliationLog).where(ReconciliationLog.run_id == run_id)
    )
    logs = log_res.scalars().all()

    return {
        "run": {
            "id": run_rec.id,
            "status": run_rec.status,
            "started_at": run_rec.started_at.isoformat() if run_rec.started_at else None,
            "completed_at": run_rec.completed_at.isoformat() if run_rec.completed_at else None,
            "transactions_checked": run_rec.transactions_checked,
            "discrepancies_found": run_rec.discrepancies_found,
            "anomalies_found": run_rec.anomalies_found,
        },
        "details": [
            {
                "id": l.id,
                "transaction_id": l.transaction_id,
                "discrepancy_type": l.discrepancy_type,
                "internal_state": l.internal_state,
                "gateway_state": l.gateway_state,
                "resolution": l.resolution,
                "requires_manual": l.requires_manual,
                "notes": l.notes,
            }
            for l in logs
        ],
    }
