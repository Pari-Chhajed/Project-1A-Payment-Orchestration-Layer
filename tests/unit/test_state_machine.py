"""
State Machine Unit Tests
Tests every valid and invalid transition.
"""
import pytest
from app.config.constants import TransactionState, VALID_TRANSITIONS
from app.services.state_machine import (
    is_valid_transition, get_valid_next_states, InvalidStateTransitionError,
)


# ── Valid transitions ─────────────────────────────────────────────
VALID_CASES = [
    (TransactionState.CREATED,            TransactionState.ROUTE_SELECTED),
    (TransactionState.CREATED,            TransactionState.ABANDONED),
    (TransactionState.ROUTE_SELECTED,     TransactionState.AUTH_INITIATED),
    (TransactionState.ROUTE_SELECTED,     TransactionState.FAILED),
    (TransactionState.AUTH_INITIATED,     TransactionState.AUTHORISED),
    (TransactionState.AUTH_INITIATED,     TransactionState.AUTH_FAILED),
    (TransactionState.AUTH_INITIATED,     TransactionState.AUTH_TIMEOUT),
    (TransactionState.AUTH_INITIATED,     TransactionState.CAPTURED),        # FS-06
    (TransactionState.AUTH_INITIATED,     TransactionState.AUTH_EXPIRED),    # FS-12
    (TransactionState.AUTHORISED,         TransactionState.CAPTURE_INITIATED),
    (TransactionState.AUTHORISED,         TransactionState.VOID_INITIATED),
    (TransactionState.AUTHORISED,         TransactionState.AUTH_EXPIRED),
    (TransactionState.AUTH_FAILED,        TransactionState.ROUTE_SELECTED),  # FS-07
    (TransactionState.AUTH_FAILED,        TransactionState.FAILED),
    (TransactionState.AUTH_TIMEOUT,       TransactionState.ROUTE_SELECTED),  # FS-07
    (TransactionState.AUTH_TIMEOUT,       TransactionState.AUTHORISED),
    (TransactionState.AUTH_TIMEOUT,       TransactionState.FAILED),
    (TransactionState.AUTH_EXPIRED,       TransactionState.FAILED),
    (TransactionState.CAPTURE_INITIATED,  TransactionState.CAPTURED),
    (TransactionState.CAPTURE_INITIATED,  TransactionState.PARTIALLY_CAPTURED),  # FS-05
    (TransactionState.CAPTURE_INITIATED,  TransactionState.CAPTURE_FAILED),       # FS-04
    (TransactionState.CAPTURED,           TransactionState.REFUND_INITIATED),
    (TransactionState.CAPTURED,           TransactionState.SETTLED),
    (TransactionState.CAPTURED,           TransactionState.DISPUTE_OPENED),
    (TransactionState.CAPTURED,           TransactionState.RECONCILIATION_MISMATCH),
    (TransactionState.PARTIALLY_CAPTURED, TransactionState.CAPTURE_INITIATED),
    (TransactionState.PARTIALLY_CAPTURED, TransactionState.REFUND_INITIATED),
    (TransactionState.PARTIALLY_CAPTURED, TransactionState.SETTLED),
    (TransactionState.CAPTURE_FAILED,     TransactionState.CAPTURE_INITIATED),
    (TransactionState.CAPTURE_FAILED,     TransactionState.VOID_INITIATED),
    (TransactionState.VOID_INITIATED,     TransactionState.VOIDED),
    (TransactionState.REFUND_INITIATED,   TransactionState.REFUNDED),
    (TransactionState.REFUND_INITIATED,   TransactionState.PARTIALLY_REFUNDED),
    (TransactionState.REFUND_INITIATED,   TransactionState.REFUND_FAILED),
    (TransactionState.PARTIALLY_REFUNDED, TransactionState.REFUND_INITIATED),
    (TransactionState.SETTLED,            TransactionState.REFUND_INITIATED),  # FS-08
    (TransactionState.SETTLED,            TransactionState.DISPUTE_OPENED),
    (TransactionState.DISPUTE_OPENED,     TransactionState.DISPUTE_RESOLVED),
    (TransactionState.DISPUTE_OPENED,     TransactionState.REFUNDED),
    (TransactionState.RECONCILIATION_MISMATCH, TransactionState.CAPTURED),
    (TransactionState.RECONCILIATION_MISMATCH, TransactionState.FAILED),
]

@pytest.mark.parametrize("from_state,to_state", VALID_CASES)
def test_valid_transition(from_state, to_state):
    assert is_valid_transition(from_state, to_state), \
        f"Expected {from_state.value} → {to_state.value} to be VALID"


# ── Invalid transitions ───────────────────────────────────────────
INVALID_CASES = [
    (TransactionState.CREATED,    TransactionState.CAPTURED),    # FS-15
    (TransactionState.CREATED,    TransactionState.REFUNDED),    # FS-15
    (TransactionState.CREATED,    TransactionState.AUTHORISED),
    (TransactionState.CREATED,    TransactionState.FAILED),
    (TransactionState.CAPTURED,   TransactionState.AUTH_INITIATED),
    (TransactionState.CAPTURED,   TransactionState.CREATED),
    (TransactionState.CAPTURED,   TransactionState.ROUTE_SELECTED),
    (TransactionState.FAILED,     TransactionState.CAPTURED),    # terminal
    (TransactionState.FAILED,     TransactionState.ROUTE_SELECTED),
    (TransactionState.REFUNDED,   TransactionState.CAPTURED),    # terminal
    (TransactionState.REFUNDED,   TransactionState.REFUND_INITIATED),
    (TransactionState.VOIDED,     TransactionState.AUTHORISED),  # terminal
    (TransactionState.ABANDONED,  TransactionState.AUTH_INITIATED),
    (TransactionState.AUTHORISED, TransactionState.AUTH_INITIATED),
    (TransactionState.AUTHORISED, TransactionState.ROUTE_SELECTED),
]

@pytest.mark.parametrize("from_state,to_state", INVALID_CASES)
def test_invalid_transition(from_state, to_state):
    assert not is_valid_transition(from_state, to_state), \
        f"Expected {from_state.value} → {to_state.value} to be INVALID"


# ── Terminal states ───────────────────────────────────────────────
def test_terminal_states_have_no_outgoing():
    terminal = [
        TransactionState.FAILED, TransactionState.REFUNDED,
        TransactionState.ABANDONED, TransactionState.VOIDED,
        TransactionState.DISPUTE_RESOLVED,
    ]
    for state in terminal:
        nexts = get_valid_next_states(state)
        assert nexts == [], f"{state.value} should be terminal (no outgoing transitions)"


# ── get_valid_next_states ─────────────────────────────────────────
def test_created_has_two_valid_next_states():
    nexts = get_valid_next_states(TransactionState.CREATED)
    assert TransactionState.ROUTE_SELECTED in nexts
    assert TransactionState.ABANDONED in nexts
    assert len(nexts) == 2


def test_captured_allows_refund_and_settle():
    nexts = get_valid_next_states(TransactionState.CAPTURED)
    assert TransactionState.REFUND_INITIATED in nexts
    assert TransactionState.SETTLED in nexts


# ── FS-15: Error message lists valid transitions ──────────────────
def test_invalid_transition_error_message():
    err = InvalidStateTransitionError(TransactionState.CREATED, TransactionState.REFUNDED)
    assert "ROUTE_SELECTED" in str(err)
    assert "CREATED" in str(err)
    assert "REFUNDED" in str(err)
    assert err.from_state == TransactionState.CREATED
    assert err.to_state == TransactionState.REFUNDED


# ── FS-12: UPI timeout path ───────────────────────────────────────
def test_upi_timeout_path():
    assert is_valid_transition(TransactionState.AUTH_INITIATED, TransactionState.AUTH_EXPIRED)
    assert is_valid_transition(TransactionState.AUTH_EXPIRED, TransactionState.FAILED)


# ── FS-08: Refund on settled ──────────────────────────────────────
def test_settled_allows_refund():
    assert is_valid_transition(TransactionState.SETTLED, TransactionState.REFUND_INITIATED)


# ── FS-04: Capture retry path ─────────────────────────────────────
def test_capture_retry_path():
    assert is_valid_transition(TransactionState.CAPTURE_INITIATED, TransactionState.CAPTURE_FAILED)
    assert is_valid_transition(TransactionState.CAPTURE_FAILED, TransactionState.CAPTURE_INITIATED)


# ── FS-05: Partial capture ────────────────────────────────────────
def test_partial_capture_path():
    assert is_valid_transition(TransactionState.CAPTURE_INITIATED, TransactionState.PARTIALLY_CAPTURED)
    assert is_valid_transition(TransactionState.PARTIALLY_CAPTURED, TransactionState.REFUND_INITIATED)
    assert is_valid_transition(TransactionState.PARTIALLY_CAPTURED, TransactionState.CAPTURE_INITIATED)


# ── FS-07: Cascade failover path ─────────────────────────────────
def test_cascade_failover_path():
    assert is_valid_transition(TransactionState.AUTH_TIMEOUT, TransactionState.ROUTE_SELECTED)
    assert is_valid_transition(TransactionState.AUTH_FAILED, TransactionState.ROUTE_SELECTED)


# ── FS-06: Webhook before API response ───────────────────────────
def test_webhook_before_api_response():
    # AUTH_INITIATED → CAPTURED is valid (UPI or webhook arrives first)
    assert is_valid_transition(TransactionState.AUTH_INITIATED, TransactionState.CAPTURED)