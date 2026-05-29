# Webhook Lifecycle

```mermaid
sequenceDiagram
  participant Gateway
  participant API
  participant Queue
  participant Worker
  participant DB
  Gateway->>API: signed webhook
  API->>API: verify signature and parse event id
  API->>Queue: persist webhook event
  alt inline processing enabled
    API->>DB: dedupe + validate amount/currency/reference
    API->>DB: state transition + audit
  else worker processing
    Worker->>Queue: claim batch with SKIP LOCKED
    Worker->>DB: dedupe + transition + audit
  end
```

Webhook guarantees:

- At-least-once delivery is handled through `processed_webhook_events`.
- Replay attacks fail HMAC verification.
- Amount and currency mismatches are rejected and logged to `security_audit_log`.
- Out-of-order events become no-op or reconciliation-required paths instead of corrupting state.
- Queue rows progress through `PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`, and `DLQ`.
