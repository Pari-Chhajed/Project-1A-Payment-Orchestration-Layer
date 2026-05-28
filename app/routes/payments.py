from fastapi import APIRouter, Depends, Header, Request, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field
from typing import Optional
import logging

from app.config.database import get_db
from app.config.constants import PaymentMethod
from app.middleware.auth import make_error_response
from app.services import payment_service as svc
from app.services.state_machine import get_timeline, InvalidStateTransitionError
from app.services.idempotency import IdempotencyConflictError
from app.utils.money import rupees_to_paise

router = APIRouter(prefix="/api/v1/payments", tags=["Payments"])
logger = logging.getLogger(__name__)


# ── Pydantic schemas ──────────────────────────────────────────────
class CustomerSchema(BaseModel):
    id: str
    email: str
    phone: str


class CreatePaymentRequest(BaseModel):
    merchant_order_id: str
    amount: int = Field(..., description="Amount in paise (integer). e.g. ₹100 = 10000")
    currency: str = Field(default="INR")
    payment_method: PaymentMethod
    customer: CustomerSchema
    description: Optional[str] = None
    metadata: Optional[dict] = None


class CaptureRequest(BaseModel):
    amount_paise: Optional[int] = None


class RefundRequest(BaseModel):
    amount_paise: int
    reason: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_payment(
    body: CreatePaymentRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    x_mock_response: Optional[str] = Header(None, alias="X-Mock-Response"),
    x_mock_delay_ms: Optional[int] = Header(None, alias="X-Mock-Delay-Ms"),
    x_mock_gateway: Optional[str] = Header(None, alias="X-Mock-Gateway"),
    x_mock_gateway_down: Optional[str] = Header(None, alias="X-Mock-Gateway-Down"),
    x_merchant_id: Optional[str] = Header("payflow_default", alias="X-Merchant-Id"),
):
    if not idempotency_key:
        return make_error_response("MISSING_IDEMPOTENCY_KEY", "Idempotency-Key header is required", 400)

    try:
        result = await svc.create_payment(
            db=db,
            merchant_id=x_merchant_id or "payflow_default",
            merchant_order_id=body.merchant_order_id,
            amount_paise=body.amount,
            currency=body.currency,
            payment_method=body.payment_method,
            customer=body.customer.model_dump(),
            idempotency_key=idempotency_key,
            description=body.description,
            meta=body.metadata,
            mock_response="gateway-down" if x_mock_gateway_down == "true" else x_mock_response,
            mock_delay_ms=x_mock_delay_ms,
            mock_gateway=x_mock_gateway,
        )
        return {"data": result}
    except IdempotencyConflictError as e:
        return make_error_response("IDEMPOTENCY_CONFLICT", str(e), 409)
    except InvalidStateTransitionError as e:
        return make_error_response("INVALID_STATE_TRANSITION", str(e), 422,
                                    {"valid_transitions": e.valid_transitions})
    except ValueError as e:
        msg = str(e)
        if msg.startswith("VALIDATION_ERROR:"):
            return make_error_response("VALIDATION_ERROR", msg.split(":", 1)[1], 400)
        if msg.startswith("ALL_GATEWAYS_FAILED"):
            return make_error_response("ALL_GATEWAYS_FAILED", "All payment gateways unavailable", 503)
        return make_error_response("PAYMENT_FAILED", msg, 422)
    except Exception as e:
        logger.exception("Unexpected error in create_payment")
        return make_error_response("INTERNAL_ERROR", "An unexpected error occurred", 500)


@router.get("/{payment_id}")
async def get_payment(payment_id: str, db: AsyncSession = Depends(get_db)):
    try:
        result = await svc.get_transaction(db, payment_id)
        return {"data": result}
    except ValueError:
        return make_error_response("PAYMENT_NOT_FOUND", f"Payment {payment_id} not found", 404)


@router.get("")
async def list_payments(
    merchant_order_id: Optional[str] = None,
    state: Optional[str] = None,
    x_merchant_id: Optional[str] = Header("payflow_default", alias="X-Merchant-Id"),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import select
    from app.models.models import Transaction

    q = select(Transaction).where(Transaction.merchant_id == (x_merchant_id or "payflow_default"))
    if merchant_order_id:
        q = q.where(Transaction.merchant_order_id == merchant_order_id)
    if state:
        q = q.where(Transaction.state == state)
    q = q.order_by(Transaction.created_at.desc()).limit(50)

    result = await db.execute(q)
    txns = result.scalars().all()
    from app.utils.money import paise_to_rupees
    return {
        "data": [
            {
                "id": t.id,
                "merchant_order_id": t.merchant_order_id,
                "state": t.state,
                "gateway": t.gateway,
                "amount": paise_to_rupees(t.amount_paise),
                "amount_paise": t.amount_paise,
                "payment_method": t.payment_method,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in txns
        ]
    }


@router.post("/{payment_id}/capture")
async def capture_payment(
    payment_id: str,
    body: CaptureRequest,
    db: AsyncSession = Depends(get_db),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    x_mock_response: Optional[str] = Header(None, alias="X-Mock-Response"),
):
    if not idempotency_key:
        return make_error_response("MISSING_IDEMPOTENCY_KEY", "Idempotency-Key header is required", 400)
    try:
        result = await svc.capture_payment(db, payment_id, body.amount_paise, idempotency_key, x_mock_response)
        return {"data": result}
    except InvalidStateTransitionError as e:
        return make_error_response("INVALID_STATE_TRANSITION", str(e), 422)
    except ValueError as e:
        return make_error_response("CAPTURE_FAILED", str(e), 422)


@router.post("/{payment_id}/void")
async def void_payment(payment_id: str, db: AsyncSession = Depends(get_db)):
    try:
        result = await svc.void_payment(db, payment_id)
        return {"data": result}
    except InvalidStateTransitionError as e:
        return make_error_response("INVALID_STATE_TRANSITION", str(e), 422)
    except ValueError as e:
        return make_error_response("VOID_FAILED", str(e), 422)


@router.post("/{payment_id}/refund")
async def refund_payment(
    payment_id: str,
    body: RefundRequest,
    db: AsyncSession = Depends(get_db),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
):
    if not idempotency_key:
        return make_error_response("MISSING_IDEMPOTENCY_KEY", "Idempotency-Key header is required", 400)
    try:
        result = await svc.refund_payment(db, payment_id, body.amount_paise, body.reason, idempotency_key)
        return {"data": result}
    except InvalidStateTransitionError as e:
        return make_error_response("INVALID_STATE_TRANSITION", str(e), 422)
    except ValueError as e:
        msg = str(e)
        if "REFUND_NOT_ALLOWED" in msg:
            return make_error_response("REFUND_NOT_ALLOWED", msg, 422)
        return make_error_response("REFUND_FAILED", msg, 422)


@router.get("/{payment_id}/refunds")
async def list_refunds(payment_id: str, db: AsyncSession = Depends(get_db)):
    result = await svc.list_refunds(db, payment_id)
    return {"data": result}


@router.get("/{payment_id}/timeline")
async def get_timeline_route(payment_id: str, db: AsyncSession = Depends(get_db)):
    timeline = await get_timeline(db, payment_id)
    return {"data": timeline}
