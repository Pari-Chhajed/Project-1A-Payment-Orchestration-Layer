"""
Payment Orchestration Service
==============================
Ties everything together:
1. Idempotency check
2. Route selection (best gateway)
3. Gateway call with timeout + failover
4. State machine transitions
5. Audit trail
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import gateways as gw
from app.config.constants import (
    GatewayName, PaymentMethod, TransactionState, REFUNDABLE_STATES,
)
from app.models.models import (
    GatewayRoute, Refund, Transaction,
)
from app.services import (
    circuit_breaker as cb_svc,
    gateway_router,
    idempotency as idem_svc,
    rate_limiter,
    state_machine as sm,
)
from app.utils.crypto import generate_trace_id
from app.utils.money import paise_to_rupees, validate_amount

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────

async def _get_txn(db: AsyncSession, txn_id: str) -> Transaction:
    result = await db.execute(select(Transaction).where(Transaction.id == txn_id))
    txn = result.scalar_one_or_none()
    if not txn:
        raise ValueError(f"PAYMENT_NOT_FOUND:{txn_id}")
    return txn


def _txn_to_dict(txn: Transaction) -> dict:
    return {
        "id": txn.id,
        "merchant_order_id": txn.merchant_order_id,
        "state": txn.state,
        "gateway": txn.gateway,
        "gateway_reference": txn.gateway_reference,
        "amount": paise_to_rupees(txn.amount_paise),
        "amount_paise": txn.amount_paise,
        "currency": txn.currency,
        "captured_amount": paise_to_rupees(txn.captured_amount_paise),
        "captured_amount_paise": txn.captured_amount_paise,
        "remaining_hold_amount_paise": max(txn.amount_paise - txn.captured_amount_paise, 0),
        "refunded_amount": paise_to_rupees(txn.refunded_amount_paise),
        "refunded_amount_paise": txn.refunded_amount_paise,
        "payment_method": txn.payment_method,
        "trace_id": txn.trace_id,
        "created_at": txn.created_at.isoformat() if txn.created_at else None,
        "updated_at": txn.updated_at.isoformat() if txn.updated_at else None,
    }


# ─────────────────────────────────────────────────────────────────
# CREATE PAYMENT
# ─────────────────────────────────────────────────────────────────

async def create_payment(
    db: AsyncSession,
    merchant_id: str,
    merchant_order_id: str,
    amount_paise: int,
    currency: str,
    payment_method: PaymentMethod,
    customer: dict,
    idempotency_key: str,
    description: str | None = None,
    meta: dict | None = None,
    mock_response: str | None = None,
    mock_delay_ms: int | None = None,
    mock_gateway: str | None = None,
) -> dict:
    trace_id = generate_trace_id()

    # Validate amount
    valid, err = validate_amount(amount_paise)
    if not valid:
        raise ValueError(f"VALIDATION_ERROR:{err}")

    # Idempotency check
    is_new, cached = await idem_svc.check_and_lock(
        db, merchant_id, idempotency_key,
        {"merchant_order_id": merchant_order_id, "amount": amount_paise, "payment_method": payment_method},
    )
    if not is_new and cached:
        return cached["body"]
    await db.commit()

    # Create transaction record
    txn_id = str(uuid.uuid4())
    txn = Transaction(
        id=txn_id,
        merchant_id=merchant_id,
        merchant_order_id=merchant_order_id,
        state=TransactionState.CREATED.value,
        payment_method=payment_method.value,
        amount_paise=amount_paise,
        currency=currency,
        customer_id=customer.get("id"),
        customer_email=customer.get("email"),
        customer_phone=customer.get("phone"),
        description=description,
        meta=meta or {},
        trace_id=trace_id,
    )
    db.add(txn)
    await db.flush()
    logger.info("Transaction %s created trace=%s", txn_id, trace_id)

    try:
        # Route selection
        if mock_gateway:
            selected_gateway = GatewayName(mock_gateway)
            reason = f"Mock gateway override: {mock_gateway}"
            scores_data = []
        else:
            decision = await gateway_router.select_gateway(db, payment_method, amount_paise, trace_id, txn_id)
            selected_gateway = decision.selected
            reason = decision.reason
            scores_data = [
                {
                    "gateway": s.gateway.value,
                    "total_score": s.total_score,
                    "success_rate": s.success_rate_score,
                    "latency": s.latency_score,
                    "cost": s.cost_score,
                    "health": s.health_score,
                    "fit": s.fit_score,
                }
                for s in decision.scores
            ]

        # Save routing decision
        route = GatewayRoute(
            transaction_id=txn_id,
            gateway=selected_gateway.value,
            score=scores_data[0]["total_score"] if scores_data else 0.0,
            score_breakdown=scores_data,
            selected=True,
            reason=reason,
        )
        db.add(route)

        # Update transaction with gateway
        txn.gateway = selected_gateway.value
        await db.flush()

        # Transition: CREATED → ROUTE_SELECTED
        await sm.transition(
            db, txn_id, TransactionState.ROUTE_SELECTED,
            event="ROUTE_SELECTED",
            meta={"gateway": selected_gateway.value},
            created_by="payment_service",
            trace_id=trace_id,
        )

        # Call gateway with failover
        await _call_gateway_with_failover(
            db, txn_id, selected_gateway, payment_method, amount_paise,
            currency, customer, idempotency_key, trace_id,
            [selected_gateway], mock_response, mock_delay_ms,
        )

        await db.commit()
        txn = await _get_txn(db, txn_id)
        response_body = _txn_to_dict(txn)
        await idem_svc.mark_completed(db, merchant_id, idempotency_key, txn_id, 201, response_body)
        await db.commit()
        return response_body

    except Exception as e:
        await idem_svc.mark_failed(db, merchant_id, idempotency_key)
        try:
            current = await _get_txn(db, txn_id)
            if current.state not in (TransactionState.FAILED.value, TransactionState.ABANDONED.value):
                await sm.transition(
                    db, txn_id, TransactionState.FAILED,
                    event="PAYMENT_FAILED",
                    meta={"error": str(e)},
                    created_by="payment_service",
                    trace_id=trace_id,
                )
        except Exception:
            pass
        await db.commit()
        raise


# ─────────────────────────────────────────────────────────────────
# GATEWAY CALL WITH TIMEOUT + FAILOVER
# ─────────────────────────────────────────────────────────────────

async def _call_gateway_with_failover(
    db: AsyncSession,
    txn_id: str,
    gateway: GatewayName,
    payment_method: PaymentMethod,
    amount_paise: int,
    currency: str,
    customer: dict,
    idempotency_key: str,
    trace_id: str,
    tried: list[GatewayName],
    mock_response: str | None,
    mock_delay_ms: int | None,
) -> None:
    timeout_sec = gw.GATEWAY_TIMEOUT.get(gateway, 30)
    authorise_fn = gw.AUTHORISE_FN[gateway]

    await sm.transition(
        db, txn_id, TransactionState.AUTH_INITIATED,
        event="AUTH_INITIATED",
        meta={"gateway": gateway.value, "attempt": len(tried)},
        created_by="payment_service",
        trace_id=trace_id,
    )
    await db.commit()

    start = asyncio.get_event_loop().time()
    try:
        await rate_limiter.acquire(db, gateway)
        response = await asyncio.wait_for(
            authorise_fn(
                txn_id, amount_paise, currency,
                payment_method.value, idempotency_key,
                mock_response, mock_delay_ms,
            ),
            timeout=timeout_sec,
        )
        latency_ms = int((asyncio.get_event_loop().time() - start) * 1000)

        if not response.success:
            await cb_svc.record_failure(db, gateway, payment_method.value)
            await gateway_router.record_metric(db, gateway, False, latency_ms)

            if response.error_code != "PAYMENT_DECLINED" and len(tried) < 4:
                await sm.transition(
                    db, txn_id, TransactionState.AUTH_FAILED,
                    event="GATEWAY_AUTH_RETRYABLE_FAILED",
                    gateway_response=response.raw_response,
                    meta={"error_code": response.error_code, "gateway": gateway.value},
                    created_by="payment_service",
                    trace_id=trace_id,
                )
                return await _attempt_failover(
                    db, txn_id, gateway, payment_method, amount_paise,
                    currency, customer, idempotency_key, trace_id, tried,
                    mock_response, mock_delay_ms,
                )

            await sm.transition(
                db, txn_id, TransactionState.AUTH_FAILED,
                event="GATEWAY_AUTH_FAILED",
                gateway_response=response.raw_response,
                meta={"error_code": response.error_code, "gateway": gateway.value},
                created_by="payment_service",
                trace_id=trace_id,
            )
            raise ValueError(response.error_message or "Payment declined")

        # SUCCESS
        await cb_svc.record_success(db, gateway, payment_method.value)
        await gateway_router.record_metric(db, gateway, True, latency_ms)

        txn = await _get_txn(db, txn_id)
        txn.gateway_reference = response.gateway_payment_id
        txn.gateway_order_id = response.gateway_order_id
        txn.auth_hold_expires_at = datetime.now(timezone.utc) + timedelta(days=7)
        await db.flush()

        if gateway == GatewayName.UPI:
            return
        else:
            await sm.transition(
                db, txn_id, TransactionState.AUTHORISED,
                event="GATEWAY_AUTH_SUCCESS",
                gateway_reference=response.gateway_payment_id,
                gateway_response=response.raw_response,
                created_by="payment_service",
                trace_id=trace_id,
            )

    except asyncio.TimeoutError:
        latency_ms = int((asyncio.get_event_loop().time() - start) * 1000)
        await cb_svc.record_failure(db, gateway, payment_method.value)
        await gateway_router.record_metric(db, gateway, False, latency_ms)
        logger.warning("Gateway %s timed out after %ds — initiating failover", gateway.value, timeout_sec)

        await sm.transition(
            db, txn_id, TransactionState.AUTH_TIMEOUT,
            event="GATEWAY_TIMEOUT",
            meta={"gateway": gateway.value, "latency_ms": latency_ms},
            created_by="payment_service",
            trace_id=trace_id,
        )

        if len(tried) < 4:
            return await _attempt_failover(
                db, txn_id, gateway, payment_method, amount_paise,
                currency, customer, idempotency_key, trace_id, tried,
                mock_response, mock_delay_ms,
            )
        raise ValueError("ALL_GATEWAYS_FAILED:All gateways timed out")
    except Exception as exc:
        latency_ms = int((asyncio.get_event_loop().time() - start) * 1000)
        await cb_svc.record_failure(db, gateway, payment_method.value)
        await gateway_router.record_metric(db, gateway, False, latency_ms)
        await sm.transition(
            db, txn_id, TransactionState.AUTH_FAILED,
            event="GATEWAY_AUTH_EXCEPTION",
            meta={"gateway": gateway.value, "error": str(exc), "latency_ms": latency_ms},
            created_by="payment_service",
            trace_id=trace_id,
        )
        if len(tried) < 4:
            return await _attempt_failover(
                db, txn_id, gateway, payment_method, amount_paise,
                currency, customer, idempotency_key, trace_id, tried,
                mock_response, mock_delay_ms,
            )
        raise


async def _attempt_failover(
    db, txn_id, failed_gw, payment_method, amount_paise, currency,
    customer, idempotency_key, trace_id, tried, mock_response, mock_delay_ms,
):
    next_gw = await gateway_router.get_failover_gateway(
        db, payment_method, amount_paise, trace_id, tried
    )
    if not next_gw:
        await sm.transition(
            db, txn_id, TransactionState.FAILED,
            event="ALL_GATEWAYS_FAILED",
            meta={"tried": [g.value for g in tried]},
            created_by="payment_service",
            trace_id=trace_id,
        )
        raise ValueError("ALL_GATEWAYS_FAILED:No available gateway")

    txn = await _get_txn(db, txn_id)
    txn.gateway = next_gw.value
    await db.flush()

    await sm.transition(
        db, txn_id, TransactionState.ROUTE_SELECTED,
        event="FAILOVER_ROUTE_SELECTED",
        meta={"failed_gateway": failed_gw.value, "new_gateway": next_gw.value},
        created_by="payment_service",
        trace_id=trace_id,
    )
    return await _call_gateway_with_failover(
        db, txn_id, next_gw, payment_method, amount_paise, currency,
        customer, idempotency_key, trace_id, tried + [next_gw],
        mock_response, mock_delay_ms,
    )


# ─────────────────────────────────────────────────────────────────
# CAPTURE
# ─────────────────────────────────────────────────────────────────

async def capture_payment(
    db: AsyncSession,
    txn_id: str,
    amount_paise: int | None = None,
    idempotency_key: str | None = None,
    mock_response: str | None = None,
) -> dict:
    txn = await _get_txn(db, txn_id)
    capture_amount = amount_paise or txn.amount_paise
    valid, err = validate_amount(capture_amount)
    if not valid:
        raise ValueError(f"VALIDATION_ERROR:{err}")
    if idempotency_key:
        is_new, cached = await idem_svc.check_and_lock(
            db,
            txn.merchant_id,
            f"capture:{idempotency_key}",
            {"transaction_id": txn_id, "amount": capture_amount},
        )
        if not is_new and cached:
            return cached["body"]
        await db.commit()

    remaining = txn.amount_paise - (txn.captured_amount_paise or 0)
    if capture_amount > remaining:
        raise ValueError(f"CAPTURE_EXCEEDS_HOLD:Max capturable is {remaining} paise")
    new_captured_total = (txn.captured_amount_paise or 0) + capture_amount
    is_partial = new_captured_total < txn.amount_paise
    gateway = GatewayName(txn.gateway)
    capture_fn = gw.CAPTURE_FN[gateway]

    await sm.transition(
        db, txn_id, TransactionState.CAPTURE_INITIATED,
        event="CAPTURE_INITIATED",
        created_by="payment_service",
        trace_id=txn.trace_id,
    )

    last_err = None
    for attempt in range(3):
        if attempt > 0:
            await asyncio.sleep(2 ** (attempt - 1))  # 1s, 2s backoff
        try:
            resp = await capture_fn(txn.gateway_reference, capture_amount, mock_response)
            if resp.success:
                new_state = TransactionState.PARTIALLY_CAPTURED if is_partial else TransactionState.CAPTURED
                await sm.transition(
                    db, txn_id, new_state,
                    event="PARTIAL_CAPTURE_SUCCESS" if is_partial else "GATEWAY_CAPTURE_SUCCESS",
                    gateway_reference=txn.gateway_reference,
                    gateway_response=resp.raw_response,
                    captured_amount_paise=new_captured_total,
                    created_by="payment_service",
                    trace_id=txn.trace_id,
                )
                await db.commit()
                response_body = _txn_to_dict(await _get_txn(db, txn_id))
                if idempotency_key:
                    await idem_svc.mark_completed(db, txn.merchant_id, f"capture:{idempotency_key}", txn_id, 200, response_body)
                    await db.commit()
                return response_body
            last_err = ValueError(resp.error_message or "Capture failed")
        except Exception as e:
            last_err = e

    await sm.transition(
        db, txn_id, TransactionState.CAPTURE_FAILED,
        event="CAPTURE_FAILED_MAX_RETRIES",
        meta={"error": str(last_err), "attempts": 3},
        created_by="payment_service",
        trace_id=txn.trace_id,
    )
    fetch_fn = gw.FETCH_FN.get(gateway)
    if fetch_fn and txn.gateway_reference:
        status = await fetch_fn(txn.gateway_reference)
        if status.status in {"captured", "succeeded", "success"}:
            await sm.transition(
                db, txn_id, TransactionState.CAPTURED,
                event="CAPTURE_LATE_SUCCESS_RECONCILED",
                gateway_reference=txn.gateway_reference,
                gateway_response=status.raw_response,
                captured_amount_paise=new_captured_total,
                meta={"source": "capture_status_poll"},
                created_by="payment_service",
                trace_id=txn.trace_id,
            )
            await db.commit()
            response_body = _txn_to_dict(await _get_txn(db, txn_id))
            if idempotency_key:
                await idem_svc.mark_completed(db, txn.merchant_id, f"capture:{idempotency_key}", txn_id, 200, response_body)
                await db.commit()
            return response_body
    await db.commit()
    if idempotency_key:
        await idem_svc.mark_failed(db, txn.merchant_id, f"capture:{idempotency_key}")
        await db.commit()
    raise last_err or ValueError("Capture failed after 3 attempts")


# ─────────────────────────────────────────────────────────────────
# REFUND
# ─────────────────────────────────────────────────────────────────

async def refund_payment(
    db: AsyncSession,
    txn_id: str,
    amount_paise: int,
    reason: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    txn = await _get_txn(db, txn_id)
    if idempotency_key:
        is_new, cached = await idem_svc.check_and_lock(
            db,
            txn.merchant_id,
            f"refund:{idempotency_key}",
            {"transaction_id": txn_id, "amount": amount_paise, "reason": reason},
        )
        if not is_new and cached:
            return cached["body"]
        await db.commit()

    if TransactionState(txn.state) not in REFUNDABLE_STATES:
        raise ValueError(f"REFUND_NOT_ALLOWED:Transaction in state {txn.state} cannot be refunded")

    max_refundable = txn.amount_paise - (txn.refunded_amount_paise or 0)
    if amount_paise > max_refundable:
        raise ValueError(f"REFUND_EXCEEDS_BALANCE:Max refundable is {max_refundable} paise")

    gateway = GatewayName(txn.gateway)
    refund_fn = gw.REFUND_FN[gateway]
    refund_id = str(uuid.uuid4())

    refund_rec = Refund(
        id=refund_id,
        transaction_id=txn_id,
        amount_paise=amount_paise,
        reason=reason,
        state="REFUND_INITIATED",
    )
    db.add(refund_rec)
    await db.flush()

    await sm.transition(
        db, txn_id, TransactionState.REFUND_INITIATED,
        event="REFUND_INITIATED",
        meta={"refund_id": refund_id, "amount": amount_paise},
        created_by="payment_service",
        trace_id=txn.trace_id,
    )

    resp = await refund_fn(txn.gateway_reference, amount_paise)
    if not resp.success:
        await sm.transition(
            db, txn_id, TransactionState.REFUND_FAILED,
            event="GATEWAY_REFUND_FAILED",
            gateway_response=resp.raw_response,
            created_by="payment_service",
            trace_id=txn.trace_id,
        )
        refund_rec.state = "REFUND_FAILED"
        await db.commit()
        raise ValueError(resp.error_message or "Refund failed")

    new_refunded = (txn.refunded_amount_paise or 0) + amount_paise
    is_full = new_refunded >= txn.amount_paise
    new_state = TransactionState.REFUNDED if is_full else TransactionState.PARTIALLY_REFUNDED

    txn.refunded_amount_paise = new_refunded
    refund_rec.state = "REFUNDED" if is_full else "PARTIALLY_REFUNDED"
    refund_rec.gateway_refund_id = resp.gateway_payment_id

    await sm.transition(
        db, txn_id, new_state,
        event="GATEWAY_REFUND_SUCCESS" if is_full else "GATEWAY_PARTIAL_REFUND_SUCCESS",
        gateway_reference=resp.gateway_payment_id,
        gateway_response=resp.raw_response,
        created_by="payment_service",
        trace_id=txn.trace_id,
    )
    await db.commit()
    response_body = _txn_to_dict(await _get_txn(db, txn_id))
    if idempotency_key:
        await idem_svc.mark_completed(db, txn.merchant_id, f"refund:{idempotency_key}", txn_id, 200, response_body)
        await db.commit()
    return response_body


# ─────────────────────────────────────────────────────────────────
# VOID
# ─────────────────────────────────────────────────────────────────

async def void_payment(db: AsyncSession, txn_id: str) -> dict:
    txn = await _get_txn(db, txn_id)
    gateway = GatewayName(txn.gateway)
    void_fn = gw.VOID_FN[gateway]

    await sm.transition(
        db, txn_id, TransactionState.VOID_INITIATED,
        event="VOID_INITIATED",
        created_by="payment_service",
        trace_id=txn.trace_id,
    )
    resp = await void_fn(txn.gateway_reference)
    await sm.transition(
        db, txn_id, TransactionState.VOIDED,
        event="GATEWAY_VOID_SUCCESS",
        gateway_response=resp.raw_response,
        created_by="payment_service",
        trace_id=txn.trace_id,
    )
    await db.commit()
    return _txn_to_dict(await _get_txn(db, txn_id))


# ─────────────────────────────────────────────────────────────────
# GET TRANSACTION
# ─────────────────────────────────────────────────────────────────

async def get_transaction(db: AsyncSession, txn_id: str) -> dict:
    txn = await _get_txn(db, txn_id)
    return _txn_to_dict(txn)


async def get_transaction_by_order_id(db: AsyncSession, merchant_id: str, merchant_order_id: str) -> dict | None:
    result = await db.execute(
        select(Transaction).where(
            Transaction.merchant_id == merchant_id,
            Transaction.merchant_order_id == merchant_order_id,
        )
    )
    txn = result.scalar_one_or_none()
    return _txn_to_dict(txn) if txn else None


async def list_refunds(db: AsyncSession, txn_id: str) -> list[dict]:
    result = await db.execute(select(Refund).where(Refund.transaction_id == txn_id))
    refunds = result.scalars().all()
    return [
        {
            "id": r.id,
            "transaction_id": r.transaction_id,
            "state": r.state,
            "amount": paise_to_rupees(r.amount_paise),
            "amount_paise": r.amount_paise,
            "gateway_refund_id": r.gateway_refund_id,
            "reason": r.reason,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in refunds
    ]
