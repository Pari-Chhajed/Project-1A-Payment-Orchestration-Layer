from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.config.database import get_db
from app.middleware.auth import make_error_response
from app.services import webhook_service as wh
from app.services.webhook_service import WebhookSignatureError, WebhookValidationError

router = APIRouter(prefix="/api/v1/webhooks", tags=["Webhooks"])


@router.post("/razorpay")
async def razorpay_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_razorpay_signature: Optional[str] = Header(None, alias="X-Razorpay-Signature"),
):
    raw_body = (await request.body()).decode("utf-8")
    source_ip = request.client.host if request.client else "0.0.0.0"
    try:
        result = await wh.process_razorpay(db, raw_body, x_razorpay_signature or "", source_ip)
        await db.commit()
        return {"received": True, **result}
    except WebhookSignatureError as e:
        return make_error_response("INVALID_SIGNATURE", str(e), 401)
    except WebhookValidationError as e:
        return make_error_response("WEBHOOK_VALIDATION_ERROR", str(e), 400)
    except Exception:
        # Always return 200 to prevent gateway retries on our bugs
        return {"received": True, "error": "Processing error"}


@router.post("/stripe")
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    stripe_signature: Optional[str] = Header(None, alias="Stripe-Signature"),
):
    raw_body = (await request.body()).decode("utf-8")
    source_ip = request.client.host if request.client else "0.0.0.0"
    try:
        result = await wh.process_stripe(db, raw_body, stripe_signature or "", source_ip)
        await db.commit()
        return {"received": True, **result}
    except WebhookSignatureError as e:
        return make_error_response("INVALID_SIGNATURE", str(e), 401)
    except Exception:
        return {"received": True, "error": "Processing error"}


@router.post("/payu")
async def payu_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    # PayU sends form-encoded body
    raw_body = (await request.body()).decode("utf-8")
    source_ip = request.client.host if request.client else "0.0.0.0"
    try:
        result = await wh.process_payu(db, raw_body, source_ip)
        await db.commit()
        return {"received": True, **result}
    except WebhookSignatureError as e:
        return make_error_response("INVALID_SIGNATURE", str(e), 401)
    except Exception:
        return {"received": True, "error": "Processing error"}


@router.post("/upi")
async def upi_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_upi_signature: Optional[str] = Header(None, alias="X-UPI-Signature"),
):
    raw_body = (await request.body()).decode("utf-8")
    source_ip = request.client.host if request.client else "0.0.0.0"
    try:
        result = await wh.process_upi(db, raw_body, x_upi_signature or "", source_ip)
        await db.commit()
        return {"received": True, **result}
    except WebhookSignatureError as e:
        return make_error_response("INVALID_SIGNATURE", str(e), 401)
    except Exception:
        return {"received": True, "error": "Processing error"}