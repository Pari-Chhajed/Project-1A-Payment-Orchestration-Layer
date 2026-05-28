"""
Webhook Processing Pipeline
============================
1. Signature verification  (reject if invalid — security)
2. Deduplication           (skip if already processed)
3. Payload validation      (amount / txn-id cross-check — prevents FS-10)
4. State machine transition
5. Immutable audit log
"""
import json
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.constants import GatewayName, TransactionState
from app.config.settings import settings
from app.models.models import ProcessedWebhookEvent, SecurityAuditLog, Transaction, WebhookQueue
from app.services import state_machine as sm
from app.utils.crypto import (
    hash_payload, verify_payu_signature,
    verify_razorpay_signature, verify_stripe_signature, verify_upi_signature,
)

logger = logging.getLogger(__name__)


class WebhookSignatureError(Exception):
    status_code = 401


class WebhookValidationError(Exception):
    status_code = 400


# ── Event → state mapping ─────────────────────────────────────────
RAZORPAY_MAP = {
    "payment.authorized": (TransactionState.AUTHORISED,         "GATEWAY_AUTH_SUCCESS"),
    "payment.captured":   (TransactionState.CAPTURED,           "GATEWAY_CAPTURE_SUCCESS"),
    "payment.failed":     (TransactionState.AUTH_FAILED,        "GATEWAY_AUTH_FAILED"),
    "refund.processed":   (TransactionState.REFUNDED,           "GATEWAY_REFUND_SUCCESS"),
    "refund.failed":      (TransactionState.REFUND_FAILED,      "GATEWAY_REFUND_FAILED"),
    "dispute.created":    (TransactionState.DISPUTE_OPENED,     "DISPUTE_CREATED"),
    "dispute.resolved":   (TransactionState.DISPUTE_RESOLVED,   "DISPUTE_RESOLVED"),
    "payment.expired":    (TransactionState.AUTH_EXPIRED,       "GATEWAY_AUTH_EXPIRED"),
}

STRIPE_MAP = {
    "payment_intent.amount_capturable_updated": (TransactionState.AUTHORISED,       "GATEWAY_AUTH_SUCCESS"),
    "payment_intent.succeeded":                 (TransactionState.CAPTURED,         "GATEWAY_CAPTURE_SUCCESS"),
    "payment_intent.payment_failed":            (TransactionState.AUTH_FAILED,      "GATEWAY_AUTH_FAILED"),
    "charge.refunded":                          (TransactionState.REFUNDED,         "GATEWAY_REFUND_SUCCESS"),
    "charge.dispute.created":                   (TransactionState.DISPUTE_OPENED,   "DISPUTE_CREATED"),
    "charge.dispute.closed":                    (TransactionState.DISPUTE_RESOLVED, "DISPUTE_RESOLVED"),
}

PAYU_MAP = {
    "success": (TransactionState.CAPTURED,    "GATEWAY_CAPTURE_SUCCESS"),
    "failure": (TransactionState.AUTH_FAILED, "GATEWAY_AUTH_FAILED"),
    "refund":  (TransactionState.REFUNDED,    "GATEWAY_REFUND_SUCCESS"),
}

UPI_MAP = {
    "SUCCESS": (TransactionState.CAPTURED,      "GATEWAY_UPI_SUCCESS"),
    "FAILURE": (TransactionState.AUTH_FAILED,   "GATEWAY_UPI_FAILED"),
    "EXPIRED": (TransactionState.AUTH_EXPIRED,  "GATEWAY_UPI_EXPIRED"),
}


async def _log_security_event(db: AsyncSession, event_type: str, gateway: str,
                               source_ip: str, raw_body: str, details: dict = None) -> None:
    try:
        log = SecurityAuditLog(
            event_type=event_type,
            gateway=gateway,
            source_ip=source_ip,
            payload_hash=hash_payload(raw_body),
            details=details or {},
        )
        db.add(log)
        await db.flush()
    except Exception as e:
        logger.error("Failed to write security audit log: %s", e)


async def _enqueue_webhook(
    db: AsyncSession,
    gateway: GatewayName,
    event_id: str,
    payload: dict,
    raw_body: str,
    signature: str,
) -> WebhookQueue:
    item = WebhookQueue(
        gateway=gateway.value,
        event_id=event_id,
        payload=payload,
        raw_body=raw_body,
        signature=signature,
        status="PENDING",
        max_retries=settings.WEBHOOK_MAX_RETRIES,
    )
    db.add(item)
    await db.flush()
    return item


async def _complete_queue_item(db: AsyncSession, item: WebhookQueue) -> None:
    item.status = "COMPLETED"
    item.processed_at = datetime.now(timezone.utc)
    await db.flush()


async def _fail_queue_item(db: AsyncSession, item: WebhookQueue, exc: Exception) -> None:
    item.retry_count += 1
    item.error_message = str(exc)
    if item.retry_count >= item.max_retries:
        item.status = "DLQ"
    else:
        item.status = "RETRY"
        delay = settings.WEBHOOK_RETRY_BASE_SECONDS ** item.retry_count
        item.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    await db.flush()


async def _process(
    db: AsyncSession,
    gateway: GatewayName,
    event_id: str,
    event_type: str,
    raw_body: str,
    event_map: dict,
    gateway_payment_id: str,
    gateway_order_id: str,
    payload_data: dict,
    source_ip: str,
) -> dict:
    payload_hash = hash_payload(raw_body)

    # ── 2. DEDUPLICATION ─────────────────────────────────────────
    existing = await db.execute(
        select(ProcessedWebhookEvent).where(
            ProcessedWebhookEvent.gateway == gateway.value,
            ProcessedWebhookEvent.event_id == event_id,
        )
    )
    if existing.scalar_one_or_none():
        logger.info("Duplicate webhook gateway=%s event_id=%s", gateway.value, event_id)
        return {"processed": False, "duplicate": True, "message": "Already processed"}

    # ── 3. Find transaction ───────────────────────────────────────
    result = await db.execute(
        select(Transaction).where(
            (Transaction.gateway_reference == gateway_payment_id) |
            (Transaction.gateway_order_id == gateway_order_id),
            Transaction.gateway == gateway.value,
        )
    )
    txn = result.scalar_one_or_none()

    # ── 4. Amount cross-check (prevents FS-10 replay fraud) ──────
    if txn:
        wh_amount = _extract_amount(payload_data, gateway)
        if wh_amount is not None and abs(wh_amount - txn.amount_paise) > 1:
            await _log_security_event(
                db, "WEBHOOK_AMOUNT_MISMATCH", gateway.value, source_ip, raw_body,
                {"transaction_id": txn.id,
                 "expected": txn.amount_paise, "got": wh_amount},
            )
            logger.error(
                "SECURITY: Amount mismatch txn=%s expected=%d got=%d",
                txn.id, txn.amount_paise, wh_amount,
            )
            raise WebhookValidationError(
                f"Amount mismatch: expected {txn.amount_paise} paise, got {wh_amount}"
            )

    # ── 5. State transition ───────────────────────────────────────
    transition_info = event_map.get(event_type)
    if txn and transition_info:
        target_state, event_name = transition_info
        current = TransactionState(txn.state)
        if sm.is_valid_transition(current, target_state):
            await sm.transition(
                db, txn.id, target_state,
                event=event_name,
                gateway_reference=gateway_payment_id,
                gateway_response=payload_data,
                meta={"source": "webhook", "event_type": event_type, "source_ip": source_ip},
                created_by="webhook_processor",
                trace_id=txn.trace_id,
            )
        else:
            # Out-of-order delivery — log and ignore gracefully (FS-06)
            logger.warning(
                "Webhook transition %s→%s not valid for txn=%s, ignoring",
                current.value, target_state.value, txn.id,
            )

    # ── 6. Mark as processed (dedup insert) ───────────────────────
    processed = ProcessedWebhookEvent(
        gateway=gateway.value,
        event_id=event_id,
        event_type=event_type,
        payload_hash=payload_hash,
        transaction_id=txn.id if txn else None,
    )
    db.add(processed)
    await db.flush()

    logger.info("Webhook processed gateway=%s event=%s event_id=%s", gateway.value, event_type, event_id)
    return {
        "processed": True,
        "duplicate": False,
        "transaction_id": txn.id if txn else None,
        "new_state": transition_info[0].value if txn and transition_info else None,
        "message": "Processed",
    }


def _extract_amount(payload: dict, gateway: GatewayName) -> int | None:
    try:
        if gateway == GatewayName.RAZORPAY:
            return payload.get("amount")
        if gateway == GatewayName.STRIPE:
            return payload.get("amount")
        if gateway in (GatewayName.PAYU, GatewayName.UPI):
            amt = payload.get("amount")
            if amt:
                return round(float(amt) * 100)
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────
# GATEWAY-SPECIFIC ENTRY POINTS
# ─────────────────────────────────────────────────────────────────

async def process_razorpay(db: AsyncSession, raw_body: str, signature: str, source_ip: str) -> dict:
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not verify_razorpay_signature(raw_body, signature, secret):
        await _log_security_event(db, "WEBHOOK_SIGNATURE_FAIL", "razorpay", source_ip, raw_body)
        raise WebhookSignatureError("Invalid Razorpay webhook signature")

    payload = json.loads(raw_body)
    event_type: str = payload.get("event", "")
    entity = (payload.get("payload") or {}).get("payment", {}).get("entity") or \
             (payload.get("payload") or {}).get("refund", {}).get("entity") or {}
    payment_id: str = entity.get("id", "")
    order_id: str = entity.get("order_id", "")
    event_id = f"{event_type}_{payment_id}"
    queue_item = await _enqueue_webhook(db, GatewayName.RAZORPAY, event_id, payload, raw_body, signature)
    try:
        result = await _process(db, GatewayName.RAZORPAY, event_id, event_type, raw_body,
                                RAZORPAY_MAP, payment_id, order_id, entity, source_ip)
        await _complete_queue_item(db, queue_item)
        return result
    except Exception as exc:
        await _fail_queue_item(db, queue_item, exc)
        raise


async def process_stripe(db: AsyncSession, raw_body: str, stripe_header: str, source_ip: str) -> dict:
    secret = settings.STRIPE_WEBHOOK_SECRET
    if not verify_stripe_signature(raw_body, stripe_header, secret):
        await _log_security_event(db, "WEBHOOK_SIGNATURE_FAIL", "stripe", source_ip, raw_body)
        raise WebhookSignatureError("Invalid Stripe webhook signature")

    payload = json.loads(raw_body)
    event_id: str = payload.get("id", "")
    event_type: str = payload.get("type", "")
    data_obj = payload.get("data", {}).get("object", {})
    gateway_id: str = data_obj.get("id", "")
    queue_item = await _enqueue_webhook(db, GatewayName.STRIPE, event_id, payload, raw_body, stripe_header)
    try:
        result = await _process(db, GatewayName.STRIPE, event_id, event_type, raw_body,
                                STRIPE_MAP, gateway_id, gateway_id, data_obj, source_ip)
        await _complete_queue_item(db, queue_item)
        return result
    except Exception as exc:
        await _fail_queue_item(db, queue_item, exc)
        raise


async def process_payu(db: AsyncSession, raw_body: str, source_ip: str) -> dict:
    payload: dict = dict(p.split("=", 1) for p in raw_body.split("&") if "=" in p)
    salt = settings.PAYU_WEBHOOK_SECRET
    received_hash = payload.get("hash", "")
    if not verify_payu_signature(payload, received_hash, salt):
        await _log_security_event(db, "WEBHOOK_SIGNATURE_FAIL", "payu", source_ip, raw_body)
        raise WebhookSignatureError("Invalid PayU webhook signature")

    event_type = payload.get("status", "unknown")
    gateway_ref = payload.get("mihpayid") or payload.get("txnid", "")
    event_id = f"{payload.get('txnid', '')}_{event_type}"
    queue_item = await _enqueue_webhook(db, GatewayName.PAYU, event_id, payload, raw_body, received_hash)
    try:
        result = await _process(db, GatewayName.PAYU, event_id, event_type, raw_body,
                                PAYU_MAP, gateway_ref, payload.get("txnid", ""), payload, source_ip)
        await _complete_queue_item(db, queue_item)
        return result
    except Exception as exc:
        await _fail_queue_item(db, queue_item, exc)
        raise


async def process_upi(db: AsyncSession, raw_body: str, signature: str, source_ip: str) -> dict:
    secret = settings.UPI_WEBHOOK_SECRET
    if not verify_upi_signature(raw_body, signature, secret):
        await _log_security_event(db, "WEBHOOK_SIGNATURE_FAIL", "upi", source_ip, raw_body)
        raise WebhookSignatureError("Invalid UPI webhook signature")

    payload = json.loads(raw_body)
    event_type: str = payload.get("status", "UNKNOWN")
    gateway_ref: str = payload.get("txnRefNum") or payload.get("upiTransactionId", "")
    event_id = f"{gateway_ref}_{event_type}"
    queue_item = await _enqueue_webhook(db, GatewayName.UPI, event_id, payload, raw_body, signature)
    try:
        result = await _process(db, GatewayName.UPI, event_id, event_type, raw_body,
                                UPI_MAP, gateway_ref, gateway_ref, payload, source_ip)
        await _complete_queue_item(db, queue_item)
        return result
    except Exception as exc:
        await _fail_queue_item(db, queue_item, exc)
        raise
