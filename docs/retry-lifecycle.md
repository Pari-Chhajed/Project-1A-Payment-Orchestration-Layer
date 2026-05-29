# Retry Lifecycle

```mermaid
sequenceDiagram
  participant API
  participant DB
  participant Gateway
  API->>DB: create transaction + idempotency PROCESSING
  API->>DB: ROUTE_SELECTED audit
  API->>DB: AUTH_INITIATED audit and commit
  API->>Gateway: authorise without holding DB transaction
  alt timeout or transient failure
    API->>DB: record gateway attempt failure
    API->>DB: AUTH_TIMEOUT then ROUTE_SELECTED
    API->>Gateway: fail over to next eligible gateway
  else success
    API->>DB: AUTHORISED or CAPTURED
    API->>DB: idempotency COMPLETED
  end
```

Retries are replay-safe because every operation is attached to a payment attempt and every gateway interaction is recorded as a gateway attempt. Hard declines do not fail over; timeouts, rate limits, and gateway unreachability may fail over or be queued depending on the operation.
