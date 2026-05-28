# Transaction State Machine

The payment lifecycle is guarded by `app/config/constants.py` and enforced by `app/services/state_machine.py`.

## Required States

- `CREATED`: transaction record exists, no gateway call yet.
- `ROUTE_SELECTED`: routing engine selected a gateway.
- `AUTH_INITIATED`: authorisation/collect request sent to the gateway.
- `AUTHORISED`: gateway placed an authorisation hold.
- `AUTH_FAILED`: gateway declined or returned a retryable auth failure.
- `AUTH_TIMEOUT`: gateway did not respond within the timeout window.
- `CAPTURE_INITIATED`: merchant requested capture against an authorisation.
- `CAPTURED`: funds are captured.
- `PARTIALLY_CAPTURED`: only part of the authorised amount is captured; remaining hold is tracked in API responses.
- `CAPTURE_FAILED`: capture failed after retries; gateway status is polled for late success.
- `REFUND_INITIATED`: refund request was sent to the gateway.
- `REFUNDED`: full refund completed.
- `PARTIALLY_REFUNDED`: partial refund completed and more refund may still be possible.
- `REFUND_FAILED`: refund failed and can be retried.
- `FAILED`: terminal failure.

## Additional States

- `VOID_INITIATED` and `VOIDED`: explicit cancellation of unused authorisation holds.
- `AUTH_EXPIRED`: authorisation/UPI collect window expired.
- `SETTLED`: gateway settlement completed.
- `ABANDONED`: customer dropped off before payment completion.
- `DISPUTE_OPENED` and `DISPUTE_RESOLVED`: chargeback lifecycle.
- `RECONCILIATION_MISMATCH`: internal and gateway states disagree and need review or override.

## Audit Rule

Every accepted or rejected transition writes to `transaction_state_log`, including `REJECTED_TRANSITION` for corruption attempts such as `CREATED -> REFUNDED`.
