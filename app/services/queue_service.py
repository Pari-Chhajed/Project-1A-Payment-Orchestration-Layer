import random
import socket
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.models import QueueMessage, WebhookQueue


def _backoff_seconds(retry_count: int) -> int:
    base = settings.QUEUE_BASE_DELAY_SECONDS * (2 ** max(retry_count - 1, 0))
    jitter = random.randint(0, max(base, 1))
    return min(base + jitter, 300)


async def enqueue_message(
    db: AsyncSession,
    queue_name: str,
    message_type: str,
    payload: dict,
    *,
    max_retries: int | None = None,
) -> QueueMessage:
    msg = QueueMessage(
        queue_name=queue_name,
        message_type=message_type,
        payload=payload,
        max_retries=max_retries or settings.QUEUE_MAX_RETRIES,
    )
    db.add(msg)
    await db.flush()
    return msg


async def enqueue_webhook(
    db: AsyncSession,
    gateway: str,
    event_id: str,
    payload: dict,
    raw_body: str,
    signature: str,
) -> WebhookQueue:
    existing = await db.execute(
        select(WebhookQueue).where(WebhookQueue.gateway == gateway, WebhookQueue.event_id == event_id)
    )
    row = existing.scalar_one_or_none()
    if row:
        return row
    item = WebhookQueue(
        gateway=gateway,
        event_id=event_id,
        payload=payload,
        raw_body=raw_body,
        signature=signature,
        max_retries=settings.QUEUE_MAX_RETRIES,
    )
    db.add(item)
    await db.flush()
    return item


async def claim_webhook_batch(db: AsyncSession, batch_size: int = 100) -> list[WebhookQueue]:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(WebhookQueue)
        .where(WebhookQueue.status.in_(["PENDING", "FAILED"]), WebhookQueue.next_retry_at <= now)
        .order_by(WebhookQueue.created_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    rows = list(result.scalars().all())
    worker_id = socket.gethostname()
    for row in rows:
        row.status = "PROCESSING"
        row.locked_at = now
        row.locked_by = worker_id
    await db.flush()
    return rows


async def mark_webhook_completed(db: AsyncSession, item: WebhookQueue) -> None:
    item.status = "COMPLETED"
    item.processed_at = datetime.now(timezone.utc)
    item.error_message = None
    await db.flush()


async def mark_webhook_failed(db: AsyncSession, item: WebhookQueue, error: Exception) -> None:
    item.retry_count += 1
    item.error_message = str(error)
    item.status = "DLQ" if item.retry_count >= item.max_retries else "FAILED"
    item.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=_backoff_seconds(item.retry_count))
    await db.flush()
