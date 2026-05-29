from app.config.constants import TransactionState
from app.services.idempotency import _hash_body



def test_idempotency_hash_is_order_independent():
    assert _hash_body({"amount": 100, "currency": "INR"}) == _hash_body({"currency": "INR", "amount": 100})


def test_advanced_states_are_present():
    assert TransactionState.SETTLEMENT_PENDING.value == "SETTLEMENT_PENDING"
    assert TransactionState.RECONCILIATION_REQUIRED.value == "RECONCILIATION_REQUIRED"
    assert TransactionState.MANUAL_REVIEW.value == "MANUAL_REVIEW"

