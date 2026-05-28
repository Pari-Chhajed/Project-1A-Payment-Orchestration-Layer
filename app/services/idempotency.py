"""
Idempotency Service
===================
Prevents double-charges when a customer clicks Pay twice
or the network retries the same request.

Every payment carries an Idempotency-Key header (UUID).
We store it. If we see it again → return cached response, no new charge.
"""
import hashlib
import json
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import IdempotencyKey

logger = logging.getLogger(__name__)


class IdempotencyConflictError(Exception):
    """Raised when a duplicate in-flight or mismatched request is detected."""
    status_code = 409


def _hash_body(body: dict) -> str:
    normalized = json.dumps(body, sort_keys=True)
    return hashlib.sha256(normalized.encode()).hexdigest()


async def check_and_lock(
    db: AsyncSession,
    merchant_id: str,
    key: str,
    request_body: dict,
) -> tuple[bool, dict | None]:
    """
    Returns (is_new, cached_response).
    - is_new=True  → proceed with processing
    - is_new=False → return cached_response to caller
    Raises IdempotencyConflictError if request is already in-flight.
    """
    request_hash = _hash_body(request_body)
    expires = datetime.now(timezone.utc) + timedelta(hours=24)

    result = await db.execute(
        select(IdempotencyKey).where(
            IdempotencyKey.merchant_id == merchant_id,
            IdempotencyKey.key == key,
        ).with_for_update()
    )
    existing = result.scalar_one_or_none()

    if existing:
        if existing.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            # Expired — delete and re-process
            await db.delete(existing)
            await db.flush()
        elif existing.request_hash != request_hash:
            raise IdempotencyConflictError(
                "Idempotency key already used with different request body."
            )
        elif existing.status == "PROCESSING":
            raise IdempotencyConflictError(
                "Request with this idempotency key is already in progress."
            )
        elif existing.status == "COMPLETED":
            logger.info("Duplicate request detected for key=%s, returning cached response", key)
            return False, {
                "code": existing.response_code,
                "body": existing.response_body,
            }
        elif existing.status == "FAILED":
            # Allow retry
            await db.delete(existing)
            await db.flush()

    # Insert new record with PROCESSING status
    record = IdempotencyKey(
        merchant_id=merchant_id,
        key=key,
        request_hash=request_hash,
        status="PROCESSING",
        expires_at=expires,
    )
    db.add(record)
    await db.flush()
    return True, None


async def mark_completed(
    db: AsyncSession,
    merchant_id: str,
    key: str,
    transaction_id: str,
    response_code: int,
    response_body: dict,
) -> None:
    result = await db.execute(
        select(IdempotencyKey).where(
            IdempotencyKey.merchant_id == merchant_id,
            IdempotencyKey.key == key,
        )
    )
    record = result.scalar_one_or_none()
    if record:
        record.status = "COMPLETED"
        record.response_code = response_code
        record.response_body = response_body
        record.transaction_id = transaction_id
        record.updated_at = datetime.now(timezone.utc)
        await db.flush()


async def mark_failed(db: AsyncSession, merchant_id: str, key: str) -> None:
    result = await db.execute(
        select(IdempotencyKey).where(
            IdempotencyKey.merchant_id == merchant_id,
            IdempotencyKey.key == key,
        )
    )
    record = result.scalar_one_or_none()
    if record:
        record.status = "FAILED"
        record.updated_at = datetime.now(timezone.utc)
        await db.flush()