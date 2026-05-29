"""
Background Workers
==================
- Reconciliation every 15 minutes
- UPI expiry check every 1 minute
- Idempotency key cleanup every hour
"""
import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.config.database import AsyncSessionLocal
from app.config.constants import TransactionState
from app.config.settings import settings
from app.models.models import IdempotencyKey, Transaction
from app.services import reconciliation_service as recon_svc
from app.services import state_machine as sm

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def _run_reconciliation():
    logger.info("Running scheduled reconciliation...")
    async with AsyncSessionLocal() as db:
        try:
            await recon_svc.run(db, triggered_by="scheduled")
        except Exception as e:
            logger.error("Reconciliation failed: %s", e)


async def _expire_upi_transactions():
    """Auto-expire UPI collect requests older than 5 minutes."""
    async with AsyncSessionLocal() as db:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
            result = await db.execute(
                select(Transaction).where(
                    Transaction.gateway == "upi",
                    Transaction.state == TransactionState.AUTH_INITIATED.value,
                    Transaction.created_at < cutoff,
                )
            )
            for txn in result.scalars().all():
                try:
                    await sm.transition(
                        db, txn.id, TransactionState.AUTH_EXPIRED,
                        event="UPI_COLLECT_TIMEOUT",
                        meta={"expired_after_minutes": 5},
                        created_by="upi_expiry_job",
                        trace_id=txn.trace_id,
                    )
                    logger.info("UPI transaction %s expired", txn.id)
                except Exception:
                    pass
            await db.commit()
        except Exception as e:
            logger.error("UPI expiry job failed: %s", e)


async def _cleanup_idempotency_keys():
    """Delete expired idempotency keys."""
    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(
                select(IdempotencyKey).where(
                    IdempotencyKey.expires_at < datetime.now(timezone.utc)
                )
            )
            expired = result.scalars().all()
            for key in expired:
                await db.delete(key)
            await db.commit()
            if expired:
                logger.info("Cleaned up %d expired idempotency keys", len(expired))
        except Exception as e:
            logger.error("Idempotency cleanup failed: %s", e)


def start_scheduler():
    interval = settings.RECONCILIATION_INTERVAL_MINUTES
    scheduler.add_job(_run_reconciliation,    "interval", minutes=interval,   id="reconciliation")
    scheduler.add_job(_expire_upi_transactions, "interval", minutes=1,         id="upi_expiry")
    scheduler.add_job(_cleanup_idempotency_keys, "interval", hours=1,          id="idem_cleanup")
    scheduler.start()
    logger.info("Background scheduler started (reconciliation every %d min)", interval)


def stop_scheduler():
    scheduler.shutdown(wait=False)


if __name__ == "__main__":
    import asyncio

    async def _main():
        start_scheduler()
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            stop_scheduler()

    asyncio.run(_main())
