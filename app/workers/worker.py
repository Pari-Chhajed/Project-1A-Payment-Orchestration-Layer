import asyncio
import logging

from app.config.database import AsyncSessionLocal, close_db
from app.models.models import WebhookQueue
from app.services import queue_service
from app.services import webhook_service
from sqlalchemy import select

logger = logging.getLogger(__name__)


async def process_webhook_batch(batch_size: int = 100) -> int:
    async with AsyncSessionLocal() as db:
        items = await queue_service.claim_webhook_batch(db, batch_size=batch_size)
        await db.commit()

    processed = 0
    for item in items:
        async with AsyncSessionLocal() as db:
            current = (await db.execute(select(WebhookQueue).where(WebhookQueue.id == item.id))).scalar_one()
            try:
                await webhook_service.process_queued_webhook(
                    db,
                    item.gateway,
                    item.raw_body,
                    item.signature,
                    source_ip="worker",
                )
                await queue_service.mark_webhook_completed(db, current)
                await db.commit()
                processed += 1
            except Exception as exc:
                logger.exception("Webhook queue item failed id=%s", item.id)
                await queue_service.mark_webhook_failed(db, current, exc)
                await db.commit()
    return processed


async def run_forever(poll_interval_seconds: float = 1.0) -> None:
    try:
        while True:
            count = await process_webhook_batch()
            if count == 0:
                await asyncio.sleep(poll_interval_seconds)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(run_forever())
