"""
Transaction State Machine
=========================
Enforces that transactions can only move between valid states.
Any invalid transition raises InvalidStateTransitionError BEFORE the DB is touched.

Every transition is logged to an immutable audit table.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.constants import VALID_TRANSITIONS, TransactionState
from app.models.models import Transaction, TransactionStateLog
from app.utils.crypto import hash_payload

logger = logging.getLogger(__name__)


class InvalidStateTransitionError(Exception):
    def __init__(self, from_state: TransactionState, to_state: TransactionState):
        valid = [s.value for s in VALID_TRANSITIONS.get(from_state, [])]
        super().__init__(
            f"Invalid transition: {from_state.value} → {to_state.value}. "
            f"Valid from {from_state.value}: {valid}"
        )
        self.from_state = from_state
        self.to_state = to_state
        self.valid_transitions = valid


def is_valid_transition(from_state: TransactionState, to_state: TransactionState) -> bool:
    return to_state in VALID_TRANSITIONS.get(from_state, [])


def get_valid_next_states(state: TransactionState) -> list[TransactionState]:
    return VALID_TRANSITIONS.get(state, [])


def _sanitize_gateway_response(response: dict) -> dict:
    """Remove PII fields before logging (PCI-DSS requirement)."""
    pii = {"card_number", "cvv", "card_cvv", "vpa", "account_number", "ifsc"}
    sanitized = {}
    for k, v in response.items():
        if k in pii:
            sanitized[k] = "[REDACTED]"
        elif isinstance(v, dict):
            sanitized[k] = _sanitize_gateway_response(v)
        else:
            sanitized[k] = v
    return sanitized


async def transition(
    db: AsyncSession,
    transaction_id: str,
    to_state: TransactionState,
    *,
    event: str,
    gateway_reference: str | None = None,
    gateway_response: dict | None = None,
    meta: dict | None = None,
    created_by: str = "system",
    trace_id: str | None = None,
    captured_amount_paise: int | None = None,
) -> tuple[TransactionState, TransactionState]:
    """
    Atomically transition a transaction to a new state.

    Returns (from_state, to_state) on success.
    Raises InvalidStateTransitionError if the move is not allowed.
    """
    # ── Step 1: Lock and read the transaction ──────────────────
    result = await db.execute(
        select(Transaction).where(Transaction.id == transaction_id).with_for_update()
    )
    txn = result.scalar_one_or_none()
    if not txn:
        raise ValueError(f"Transaction {transaction_id} not found")

    from_state = TransactionState(txn.state)
    effective_trace = trace_id or txn.trace_id

    if from_state == to_state:
        audit = TransactionStateLog(
            transaction_id=transaction_id,
            from_state=from_state.value,
            to_state=to_state.value,
            event=f"DUPLICATE_{event}",
            gateway_reference=gateway_reference,
            gateway_response=_sanitize_gateway_response(gateway_response or {}),
            meta={**(meta or {}), "idempotent_noop": True},
            trace_id=effective_trace,
            created_by=created_by,
        )
        db.add(audit)
        await db.flush()
        return from_state, to_state

    # ── Step 2: Validate transition BEFORE any DB change ──────
    if not is_valid_transition(from_state, to_state):
        # Log the rejected attempt to the audit trail
        audit = TransactionStateLog(
            transaction_id=transaction_id,
            from_state=from_state.value,
            to_state=to_state.value,
            event="REJECTED_TRANSITION",
            meta={"attempted_event": event, "reason": "Invalid state transition"},
            trace_id=effective_trace,
            created_by=created_by,
        )
        db.add(audit)
        await db.flush()
        logger.warning(
            "Rejected invalid transition %s → %s for txn %s",
            from_state.value, to_state.value, transaction_id,
        )
        raise InvalidStateTransitionError(from_state, to_state)

    # ── Step 3: Update the transaction ──────────────────────
    txn.state = to_state.value
    txn.version += 1
    txn.updated_at = datetime.now(timezone.utc)
    if gateway_reference:
        txn.gateway_reference = gateway_reference
    if captured_amount_paise is not None:
        txn.captured_amount_paise = captured_amount_paise

    # ── Step 4: Write immutable audit log ───────────────────
    sanitized = _sanitize_gateway_response(gateway_response or {})
    audit = TransactionStateLog(
        transaction_id=transaction_id,
        from_state=from_state.value,
        to_state=to_state.value,
        event=event,
        gateway_reference=gateway_reference,
        gateway_response=sanitized,
        meta=meta or {},
        trace_id=effective_trace,
        created_by=created_by,
    )
    db.add(audit)
    await db.flush()

    logger.info(
        "Transaction %s: %s → %s [%s] trace=%s",
        transaction_id, from_state.value, to_state.value, event, effective_trace,
    )
    return from_state, to_state


async def get_timeline(db: AsyncSession, transaction_id: str) -> list[dict]:
    result = await db.execute(
        select(TransactionStateLog)
        .where(TransactionStateLog.transaction_id == transaction_id)
        .order_by(TransactionStateLog.created_at)
    )
    logs = result.scalars().all()
    return [
        {
            "id": l.id,
            "from_state": l.from_state,
            "to_state": l.to_state,
            "event": l.event,
            "gateway_reference": l.gateway_reference,
            "gateway_response": l.gateway_response,
            "meta": l.meta,
            "trace_id": l.trace_id,
            "created_at": l.created_at.isoformat() if l.created_at else None,
            "created_by": l.created_by,
        }
        for l in logs
    ]
