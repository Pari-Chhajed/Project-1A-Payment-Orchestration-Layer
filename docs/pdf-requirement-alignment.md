# PDF Requirement Alignment

This project implements the Payment Orchestration Layer requested in the assignment PDF, limited to the simulated gateway environment described by the test protocol.

## Implemented

- Transaction state machine with explicit valid transitions in `app/config/constants.py`.
- Immutable transition audit log through `transaction_state_log`.
- Multi-gateway routing using success rate, latency, cost, health, and payment-method fit.
- Per-gateway and per-payment-method circuit breaker rows.
- Idempotency store scoped by `(merchant_id, key)` for payment, capture, and refund operations.
- Mock gateway controls for success, timeout, server error, decline, rate limit, delay, and gateway-down scenarios.
- Webhook signature verification, deduplication, persistent queue records, retry/DLQ state, and security audit logging.
- Reconciliation engine for stale transaction polling and mismatch/anomaly records.
- Integer paise storage for all monetary values.
- PostgreSQL-backed Docker Compose runtime.
- Gateway rate-limit state and token-bucket acquisition before outbound gateway calls.

## Simulation Notes

- Gateway adapters intentionally remain mocks because the PDF states the automated harness cannot connect to real gateways.
- `Base.metadata.create_all` is retained so the Docker Compose simulation starts cleanly without a separate migration command.
- Secrets in `docker-compose.yml` are deterministic test values for the assignment harness and should be replaced outside the simulation.

## Key Endpoints

- `POST /api/v1/payments`
- `POST /api/v1/payments/{id}/capture`
- `POST /api/v1/payments/{id}/refund`
- `POST /api/v1/webhooks/{gateway}`
- `GET /api/v1/gateways/rate-limits/status`
- `GET /api/v1/reconciliation/webhook-dlq`
- `POST /api/v1/reconciliation/trigger`
