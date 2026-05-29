# Scaling Notes

Hot paths:

- Payment creation commits intermediate states before gateway calls.
- Webhook ingestion is decoupled from processing through a durable queue.
- Reconciliation claims batches with `SKIP LOCKED` so multiple workers can run safely.
- PgBouncer protects PostgreSQL from connection storms.

Recommended knobs:

- `DB_POOL_SIZE` and `DB_MAX_OVERFLOW` for API/worker process pools.
- `RATE_LIMIT_PER_MINUTE` for ingress backpressure.
- `QUEUE_MAX_RETRIES` and `QUEUE_BASE_DELAY_SECONDS` for poison-message handling.
- `RECONCILIATION_INTERVAL_MINUTES` for scheduler cadence.

For large datasets, partition `transaction_state_log`, `gateway_health_metrics`, `processed_webhook_events`, and `webhook_queue` by day or month.
