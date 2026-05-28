"""
Gateway Adapters
================
Each gateway (Razorpay, Stripe, PayU, UPI) has an adapter.
The orchestration layer calls only the BaseAdapter interface.

In development/testing, mock responses are controlled via
X-Mock-Response header: success | timeout | server-error | decline | rate-limit
"""
import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GatewayResponse:
    success: bool
    gateway_order_id: Optional[str] = None
    gateway_payment_id: Optional[str] = None
    status: Optional[str] = None
    amount_paise: Optional[int] = None
    currency: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    raw_response: dict = field(default_factory=dict)
    latency_ms: int = 0


@dataclass
class PaymentStatusResponse:
    gateway_payment_id: str
    status: str  # authorized | captured | failed | refunded | pending | expired
    amount_paise: Optional[int] = None
    raw_response: dict = field(default_factory=dict)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:14]}"


async def _mock(mock_response: str | None, mock_delay_ms: int | None, success: GatewayResponse) -> GatewayResponse:
    delay = (mock_delay_ms or 0) / 1000
    if delay > 0:
        await asyncio.sleep(delay)

    if mock_response == "timeout":
        await asyncio.sleep(99)  # will be interrupted by timeout
        raise TimeoutError("Gateway timeout")
    if mock_response == "server-error":
        return GatewayResponse(success=False, error_code="SERVER_ERROR",
                               error_message="Gateway internal error (502)",
                               raw_response={"status": 502})
    if mock_response == "decline":
        return GatewayResponse(success=False, error_code="PAYMENT_DECLINED",
                               error_message="Declined by issuing bank",
                               raw_response={"error_code": "INSUFFICIENT_FUNDS"})
    if mock_response == "rate-limit":
        return GatewayResponse(success=False, error_code="RATE_LIMIT_EXCEEDED",
                               error_message="Too many requests. Retry after 1s.",
                               raw_response={"status": 429, "retry_after": 1})
    if mock_response == "gateway-down":
        raise ConnectionError("Gateway unreachable")

    # Default: simulate realistic latency
    if delay == 0:
        await asyncio.sleep(0.1 + uuid.uuid4().int % 200 / 1000)
    return success


# ─────────────────────────────────────────────────────────────────
# RAZORPAY
# ─────────────────────────────────────────────────────────────────
async def razorpay_authorise(order_id: str, amount_paise: int, currency: str,
                              payment_method: str, idempotency_key: str,
                              mock_response: str | None = None,
                              mock_delay_ms: int | None = None) -> GatewayResponse:
    pay_id = _uid("pay")
    ord_id = _uid("order")
    return await _mock(mock_response, mock_delay_ms, GatewayResponse(
        success=True, gateway_order_id=ord_id, gateway_payment_id=pay_id,
        status="authorized", amount_paise=amount_paise, currency=currency,
        raw_response={"id": pay_id, "order_id": ord_id, "status": "authorized",
                      "amount": amount_paise, "currency": currency},
    ))


async def razorpay_capture(gateway_payment_id: str, amount_paise: int,
                            mock_response: str | None = None) -> GatewayResponse:
    return await _mock(mock_response, None, GatewayResponse(
        success=True, gateway_payment_id=gateway_payment_id,
        status="captured", amount_paise=amount_paise,
        raw_response={"id": gateway_payment_id, "status": "captured", "amount": amount_paise},
    ))


async def razorpay_refund(gateway_payment_id: str, amount_paise: int,
                           mock_response: str | None = None) -> GatewayResponse:
    ref_id = _uid("rfnd")
    return await _mock(mock_response, None, GatewayResponse(
        success=True, gateway_payment_id=ref_id, status="processed",
        amount_paise=amount_paise,
        raw_response={"id": ref_id, "payment_id": gateway_payment_id,
                      "amount": amount_paise, "status": "processed"},
    ))


async def razorpay_void(gateway_payment_id: str) -> GatewayResponse:
    return GatewayResponse(success=True, status="cancelled",
                            raw_response={"id": gateway_payment_id, "status": "cancelled"})


async def razorpay_fetch_status(gateway_payment_id: str) -> PaymentStatusResponse:
    return PaymentStatusResponse(gateway_payment_id=gateway_payment_id,
                                  status="captured",
                                  raw_response={"id": gateway_payment_id, "status": "captured"})


# ─────────────────────────────────────────────────────────────────
# STRIPE
# ─────────────────────────────────────────────────────────────────
async def stripe_authorise(order_id: str, amount_paise: int, currency: str,
                             payment_method: str, idempotency_key: str,
                             mock_response: str | None = None,
                             mock_delay_ms: int | None = None) -> GatewayResponse:
    pi_id = _uid("pi")
    return await _mock(mock_response, mock_delay_ms, GatewayResponse(
        success=True, gateway_order_id=pi_id, gateway_payment_id=pi_id,
        status="requires_capture", amount_paise=amount_paise, currency=currency.lower(),
        raw_response={"id": pi_id, "object": "payment_intent",
                      "status": "requires_capture", "amount": amount_paise},
    ))


async def stripe_capture(gateway_payment_id: str, amount_paise: int,
                          mock_response: str | None = None) -> GatewayResponse:
    return await _mock(mock_response, None, GatewayResponse(
        success=True, gateway_payment_id=gateway_payment_id,
        status="succeeded", amount_paise=amount_paise,
        raw_response={"id": gateway_payment_id, "status": "succeeded"},
    ))


async def stripe_refund(gateway_payment_id: str, amount_paise: int,
                         mock_response: str | None = None) -> GatewayResponse:
    ref_id = _uid("re")
    return await _mock(mock_response, None, GatewayResponse(
        success=True, gateway_payment_id=ref_id, status="succeeded",
        amount_paise=amount_paise,
        raw_response={"id": ref_id, "payment_intent": gateway_payment_id,
                      "status": "succeeded", "amount": amount_paise},
    ))


async def stripe_void(gateway_payment_id: str) -> GatewayResponse:
    return GatewayResponse(success=True, status="canceled",
                            raw_response={"id": gateway_payment_id, "status": "canceled"})


async def stripe_fetch_status(gateway_payment_id: str) -> PaymentStatusResponse:
    return PaymentStatusResponse(gateway_payment_id=gateway_payment_id,
                                  status="captured",
                                  raw_response={"id": gateway_payment_id, "status": "succeeded"})


# ─────────────────────────────────────────────────────────────────
# PAYU
# ─────────────────────────────────────────────────────────────────
async def payu_authorise(order_id: str, amount_paise: int, currency: str,
                          payment_method: str, idempotency_key: str,
                          mock_response: str | None = None,
                          mock_delay_ms: int | None = None) -> GatewayResponse:
    txn_id = _uid("payu")
    return await _mock(mock_response, mock_delay_ms, GatewayResponse(
        success=True, gateway_order_id=txn_id, gateway_payment_id=txn_id,
        status="success", amount_paise=amount_paise, currency=currency,
        raw_response={"txnid": txn_id, "status": "success",
                      "amount": f"{amount_paise/100:.2f}"},
    ))


async def payu_refund(gateway_payment_id: str, amount_paise: int,
                       mock_response: str | None = None) -> GatewayResponse:
    ref_id = _uid("payu_ref")
    return await _mock(mock_response, None, GatewayResponse(
        success=True, gateway_payment_id=ref_id, status="success",
        amount_paise=amount_paise,
        raw_response={"refundId": ref_id, "status": "success"},
    ))


async def payu_fetch_status(gateway_payment_id: str) -> PaymentStatusResponse:
    return PaymentStatusResponse(gateway_payment_id=gateway_payment_id,
                                  status="captured",
                                  raw_response={"txnid": gateway_payment_id, "status": "success"})


# ─────────────────────────────────────────────────────────────────
# UPI  (instant — no auth+capture phase)
# ─────────────────────────────────────────────────────────────────
async def upi_authorise(order_id: str, amount_paise: int, currency: str,
                         payment_method: str, idempotency_key: str,
                         mock_response: str | None = None,
                         mock_delay_ms: int | None = None) -> GatewayResponse:
    txn_ref = f"UPI{uuid.uuid4().hex[:16].upper()}"
    return await _mock(mock_response, mock_delay_ms, GatewayResponse(
        success=True, gateway_order_id=txn_ref, gateway_payment_id=txn_ref,
        status="pending", amount_paise=amount_paise, currency="INR",
        raw_response={"txnRefNum": txn_ref, "status": "PENDING",
                      "amount": f"{amount_paise/100:.2f}"},
    ))


async def upi_fetch_status(gateway_payment_id: str) -> PaymentStatusResponse:
    return PaymentStatusResponse(gateway_payment_id=gateway_payment_id,
                                  status="captured",
                                  raw_response={"txnRefNum": gateway_payment_id, "status": "SUCCESS"})


# ─────────────────────────────────────────────────────────────────
# DISPATCHER — picks the right adapter by gateway name
# ─────────────────────────────────────────────────────────────────
from app.config.constants import GatewayName  # noqa: E402

AUTHORISE_FN = {
    GatewayName.RAZORPAY: razorpay_authorise,
    GatewayName.STRIPE:   stripe_authorise,
    GatewayName.PAYU:     payu_authorise,
    GatewayName.UPI:      upi_authorise,
}

CAPTURE_FN = {
    GatewayName.RAZORPAY: razorpay_capture,
    GatewayName.STRIPE:   stripe_capture,
    GatewayName.PAYU:     lambda gid, amt, mr=None: GatewayResponse(
        success=True, gateway_payment_id=gid, status="captured",
        raw_response={"status": "captured"},
    ),
    GatewayName.UPI: lambda gid, amt, mr=None: GatewayResponse(
        success=False, error_code="NOT_SUPPORTED",
        error_message="UPI does not support separate capture",
        raw_response={},
    ),
}

REFUND_FN = {
    GatewayName.RAZORPAY: razorpay_refund,
    GatewayName.STRIPE:   stripe_refund,
    GatewayName.PAYU:     payu_refund,
    GatewayName.UPI: lambda gid, amt, mr=None: GatewayResponse(
        success=False, error_code="NOT_SUPPORTED",
        error_message="UPI refunds processed via NPCI portal",
        raw_response={},
    ),
}

VOID_FN = {
    GatewayName.RAZORPAY: razorpay_void,
    GatewayName.STRIPE:   stripe_void,
    GatewayName.PAYU:     lambda gid: GatewayResponse(success=True, raw_response={"status": "cancelled"}),
    GatewayName.UPI:      lambda gid: GatewayResponse(success=True, raw_response={"status": "EXPIRED"}),
}

FETCH_FN = {
    GatewayName.RAZORPAY: razorpay_fetch_status,
    GatewayName.STRIPE:   stripe_fetch_status,
    GatewayName.PAYU:     payu_fetch_status,
    GatewayName.UPI:      upi_fetch_status,
}

GATEWAY_TIMEOUT = {
    GatewayName.RAZORPAY: 30,
    GatewayName.STRIPE:   30,
    GatewayName.PAYU:     45,
    GatewayName.UPI:      60,
}