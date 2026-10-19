# Payment Orchestration Layer: Interview Preparation

This guide is based on the code in this repository. It deliberately separates implemented behavior from production improvements so interview answers remain accurate.

## 1. A strong project introduction

Use this answer for “Tell me about your project”:

> I built PayFlow Commerce, a payment orchestration backend using FastAPI and asynchronous SQLAlchemy. The goal is to give a merchant one consistent payment API while hiding differences between multiple payment gateways. The service supports Razorpay, Stripe, PayU, and UPI through gateway adapters. It selects an eligible gateway using a weighted score based on success rate, latency, cost, health, and payment-method fit. It also handles gateway timeouts and failover, idempotency to prevent duplicate charges, signed and deduplicated webhooks, a transaction state machine, captures, refunds, voids, reconciliation, health metrics, and circuit breakers. Amounts are stored as integer paise, and every state change is written to an audit trail. The adapters in this repository are mock adapters for development and tests; a production version would replace them with authenticated provider clients and a durable queue.

Keep the introduction under 90 seconds. Then let the interviewer choose which area to explore.

## 2. Purpose and business problem

Without an orchestration layer, every merchant has to integrate each gateway separately, understand different request and response formats, implement different webhook rules, and build its own retry, refund, reconciliation, and reporting logic. That causes duplicated code and makes failures difficult to handle consistently.

This project provides:

- one merchant-facing API;
- gateway abstraction through adapters;
- routing based on reliability, speed, cost, health, and method support;
- controlled failover when a gateway times out or returns a retryable error;
- protection against double submission;
- a canonical transaction state model across different gateway vocabularies;
- auditability for support, finance, and dispute investigations;
- reconciliation when the internal database and a gateway disagree.

The payment domain is difficult because a request can time out after the gateway has actually charged the customer, webhooks are retried and can arrive out of order, and a payment may be authorised, captured, settled, refunded, or disputed at different times.

## 3. Architecture

```text
Client / Merchant
        |
FastAPI routes + API-key middleware + request logging
        |
Payment service
  |       |             |             |
Idempotency  Router     State machine  Webhook service
  |       |             |             |
SQLite/SQLAlchemy   Gateway adapters  Reconciliation
                         |
             Razorpay | Stripe | PayU | UPI
```

### Main modules

| Area | Files | Responsibility |
|---|---|---|
| Application | `app/main.py` | FastAPI app, lifespan, middleware, exception handler, route registration |
| API | `app/routes/payments.py` | Create, get, list, capture, void, refund, refunds, timeline |
| Webhooks | `app/routes/webhooks.py` | Gateway-specific webhook endpoints |
| Payment orchestration | `app/services/payment_service.py` | End-to-end payment flow, failover, capture, refund, void |
| Routing | `app/services/gateway_router.py` | Weighted gateway scoring and metrics |
| Reliability | `app/services/circuit_breaker.py` | CLOSED, OPEN, HALF_OPEN behavior |
| Consistency | `app/services/idempotency.py` | Request hash, PROCESSING/COMPLETED/FAILED records, cached response |
| State | `app/services/state_machine.py`, `app/config/constants.py` | Allowed transitions and immutable audit log |
| Webhooks | `app/services/webhook_service.py` | HMAC/hash validation, event mapping, deduplication, amount checks |
| Finance operations | `app/services/reconciliation_service.py` | Compare internal state with gateway status |
| Integrations | `app/adapters/gateways.py` | Common response objects and gateway-specific adapter functions |
| Persistence | `app/models/models.py`, `app/config/database.py` | SQLAlchemy models and async database sessions |
| Jobs | `app/workers/scheduler.py` | Reconciliation, UPI expiry, idempotency cleanup |
| Tests | `tests/unit`, `tests/e2e` | Pure logic and API/failure-scenario coverage |

## 4. Payment flow, step by step

For `POST /api/v1/payments`:

1. Middleware checks `X-API-Key` except for health and webhook routes and adds `X-Request-ID`.
2. The route requires `Idempotency-Key` and validates the Pydantic request body.
3. `create_payment` validates that the amount is a positive integer in paise.
4. `check_and_lock` hashes the important request fields and creates a 24-hour `PROCESSING` record. A completed duplicate returns the stored response; a different body with the same key returns a conflict.
5. A `Transaction` starts in `CREATED`.
6. `gateway_router.select_gateway` filters by payment-method support and circuit-breaker status, reads recent health metrics, normalises latency and cost, and computes the weighted score.
7. The route is stored in `gateway_routes`, and the transaction moves to `ROUTE_SELECTED`.
8. The adapter call is wrapped in `asyncio.wait_for`. The state becomes `AUTH_INITIATED`.
9. A normal card-like flow becomes `AUTHORISED`; a separate capture call later moves it to `CAPTURED`. UPI is treated as an immediate captured flow in this implementation.
10. Retryable errors and timeouts record gateway failure metrics and may select another eligible gateway. A payment decline is treated as a non-retryable business result.
11. On success, the service stores the gateway IDs, records health metrics, commits the transaction, and marks the idempotency record `COMPLETED` with the response body.
12. On failure, it marks the key `FAILED`, attempts a terminal `FAILED` state transition, commits, and returns a structured error.

The important payment-domain caveat is that a timeout is an unknown outcome. A production implementation should first use the gateway idempotency key or query the original gateway transaction before blindly failing over, otherwise two gateways could charge the customer.

## 5. State machine you should be able to draw

```text
CREATED -> ROUTE_SELECTED -> AUTH_INITIATED
                              |       |
             AUTH_FAILED <----+       +----> AUTH_TIMEOUT
                |                       |           |
                +----> ROUTE_SELECTED <-+           +----> ROUTE_SELECTED
                              |
                         AUTHORISED
                              |
                    CAPTURE_INITIATED
                       |            |
                   CAPTURED   PARTIALLY_CAPTURED
                       |            |
                    SETTLED     REFUND_INITIATED
                       |            |
                    REFUND_INITIATED -> REFUNDED / PARTIALLY_REFUNDED
```

The complete enum also includes `VOID_INITIATED`, `VOIDED`, `CAPTURE_FAILED`, `REFUND_FAILED`, `AUTH_EXPIRED`, `DISPUTE_OPENED`, `DISPUTE_RESOLVED`, `RECONCILIATION_MISMATCH`, `FAILED`, and `ABANDONED`.

`state_machine.transition` reads the row with `with_for_update`, validates the transition, updates the state and version, redacts selected PII from gateway responses, and writes a `TransactionStateLog`. Invalid transitions are recorded as rejected audit events and raise `InvalidStateTransitionError`.

## 6. Routing explanation

The documented formula is:

```text
score = 0.35 * success_rate
      + 0.20 * latency_score
      + 0.20 * cost_score
      + 0.15 * health_score
      + 0.10 * fit_score
```

The default weights sum to 1.0. Latency is normalised between 100 ms and 2,000 ms. Cost is calculated as a percentage plus a fixed amount in paise. A gateway is excluded when its circuit is open or it does not support the requested method. In this repository, fit is currently a binary eligibility check and is `1.0` for an eligible gateway.

The score breakdown is persisted so a routing decision can be explained later. This matters for debugging, merchant support, and tuning conversion versus cost.

## 7. Reliability and security talking points

### Idempotency

The key is scoped by merchant and stored with a request hash. `PROCESSING` blocks a second in-flight request, `COMPLETED` returns the cached response, and a mismatched body is rejected. This prevents a network retry or double click from creating two charges.

### Circuit breaker

- `CLOSED`: requests are allowed and failures are counted.
- `OPEN`: requests fail fast until the timeout elapses.
- `HALF_OPEN`: one probe is allowed; success closes the circuit and failure reopens it.

### Webhooks

The service validates the raw request body with gateway-specific signatures, uses `hmac.compare_digest`, checks Stripe timestamp tolerance, maps gateway events into canonical states, checks the amount against the transaction, and deduplicates by `(gateway, event_id)`. This supports at-least-once delivery without applying an event twice.

### Money

Database amounts use `BigInteger` paise. Integer money avoids binary floating-point errors such as `0.1 + 0.2`. The API returns both display rupees and integer paise.

### PCI and privacy

The state-machine audit log redacts fields such as card number, CVV, VPA, account number, and IFSC. A real deployment should also avoid storing raw card data, use tokenisation, restrict logs, rotate secrets, and move secrets out of source configuration.

## 8. Data model

Know the reason for the important tables:

- `transactions`: canonical payment record, current state, amount, gateway IDs, trace ID, and version.
- `transaction_state_log`: append-only state history and event audit trail.
- `idempotency_keys`: request hash and cached response.
- `processed_webhook_events`: webhook deduplication.
- `webhook_queue`: schema for retry/DLQ processing; the current webhook routes process synchronously, so this model is not yet wired into a worker.
- `gateway_health_metrics`: request counts, success counts, failures, and latency windows.
- `circuit_breaker_state`: per-gateway breaker state and thresholds.
- `routing_config`: tunable routing weights.
- `refunds`: partial and full refund records.
- `reconciliation_runs` and `reconciliation_log`: finance checks and discrepancies.
- `security_audit_log`: signature and amount-mismatch security events.
- `gateway_routes`: why a gateway was selected.

Indexes exist on transaction state, gateway, creation time, gateway reference, trace ID, and health gateway/window. The merchant/order unique constraint prevents duplicate merchant orders.

## 9. Testing strategy in the repository

The stack is `pytest`, `pytest-asyncio`, `httpx`, and FastAPI `ASGITransport`.

### Unit tests

`tests/unit/test_state_machine.py` uses parameterisation to test valid and invalid transitions, terminal states, error contents, UPI expiry, refund, partial capture, and failover paths.

`tests/unit/test_utils.py` tests integer-money conversion, amount validation, SHA-256 hashing, HMAC signatures, Stripe timestamp expiry, malformed signatures, and tampered payloads.

### End-to-end/API tests

`tests/e2e/test_failure_scenarios.py` creates a fresh SQLite schema per test, seeds routing and circuit-breaker data, calls the FastAPI app through `httpx`, and covers the 15 labelled failure scenarios plus API validation and happy-path endpoints. The scenarios include duplicate webhooks, duplicate payment submission, capture retry, partial capture, webhook ordering, failover, refund after settlement, replay/tampering, reconciliation, UPI expiry, merchant-scoped keys, and invalid state transitions.

When explaining tests, say what behavior each test protects, not only that a line is covered. In production I would add concurrency tests, provider contract tests, property tests for state transitions, database migration tests, and fault-injection tests for “gateway charged but response timed out.”

### Useful local commands

```text
pip install -r requirements.txt
uvicorn app.main:app --reload
pytest
```

The interactive API documentation is exposed at `/docs`. A representative request is:

```json
POST /api/v1/payments
X-API-Key: test-api-key-12345
Idempotency-Key: a-client-generated-uuid

{
  "merchant_order_id": "ORDER-1001",
  "amount": 100000,
  "currency": "INR",
  "payment_method": "card",
  "customer": {"id": "CUST-1", "email": "a@example.com", "phone": "9999999999"}
}
```

`100000` means ₹1,000.00 because the API accepts paise. For local testing, `X-Mock-Response` can select `success`, `timeout`, `server-error`, `decline`, or `rate-limit`, and `X-Mock-Gateway` can force a gateway.

## 10. Honest implementation boundaries

If asked what you would improve, use these concrete points:

1. The adapters return simulated responses; replace them with real provider SDK/HTTP clients, connection timeouts, provider idempotency headers, and contract tests.
2. SQLite is useful for local tests but production should use PostgreSQL or another transactional database with a real connection pool and migration workflow.
3. The `WebhookQueue`/DLQ model exists but the current endpoints process synchronously; wire it to a durable queue and worker with exponential retry and dead-letter handling.
4. Capture and refund endpoints accept idempotency keys but the service does not yet use them to deduplicate those operations.
5. `merchant_id` is currently hardcoded and authentication is a single API key; production needs tenant identity, key rotation, authorisation, and rate limiting.
6. Timeout failover must confirm the original gateway status before trying another gateway.
7. Validate capture and refund amounts strictly: positive, not greater than the authorised/capturable or refundable balance, and consistent across repeated operations.
8. CORS is permissive, secrets have test defaults, and the global error response should be paired with structured internal observability.
9. Add outbox/event publishing, distributed tracing, metrics, alerting, and explicit process uptime.
10. The repository includes empty `Dockerfile`, `docker-compose.yml`, and documentation placeholders, so do not claim container deployment or completed design documentation unless you add and verify them.

These are safe answers because they show engineering judgement without claiming features that are not in this repository.

## 11. Likely questions and compact answers

### Why did you create an orchestration layer?

To give merchants one stable API and centralise routing, failure handling, idempotency, webhooks, refunds, and reconciliation. Adding a gateway should require an adapter rather than changes throughout the business logic.

### Why are adapters useful?

They isolate provider-specific request formats, status names, IDs, and capabilities behind common functions such as authorise, capture, refund, void, and fetch status. This is an adapter/strategy design and keeps the service testable.

### Why is idempotency different from a database unique order ID?

The order ID identifies a business order, while an idempotency key identifies a retried API operation. The key also stores the request hash and response, so the same request can safely return the original result.

### What happens when a gateway times out?

The service records a timeout, updates metrics and the breaker, and can select another eligible gateway. In production I would first query the original gateway using its idempotency key because the timeout does not prove that the charge failed.

### Can you guarantee exactly-once payment?

No. External networks and gateways are at-least-once and can have unknown outcomes. I can make internal operations idempotent, use provider idempotency, deduplicate webhooks, and reconcile until the final outcome is known.

### Why are webhooks needed if the API returns a response?

The API response is not authoritative for all later transitions. Webhooks communicate asynchronous capture, refund, dispute, expiry, and settlement events, and they can arrive after the request has completed or before the response is observed.

### How do you handle duplicate or out-of-order webhooks?

Deduplicate by gateway and event ID, validate the signature and amount, and apply only valid canonical state transitions. An already processed event returns a duplicate result; an invalid ordering is logged and ignored rather than corrupting the state.

### Explain the circuit breaker.

After repeated failures the breaker opens and prevents wasting requests on a failing gateway. After a cooldown it allows one half-open probe. Success closes it; failure opens it again.

### Why store an audit trail?

Payments need explainability for customer support, finance, disputes, compliance, and debugging. A current state alone cannot explain how the transaction got there.

### How would you scale this system?

Run stateless API instances behind a load balancer, use PostgreSQL with proper row locks, move webhooks and reconciliation to durable queues/workers, cache read-only routing configuration, partition or archive audit data, use metrics/traces, and preserve idempotency in the shared database.

### How would you secure it?

TLS, rotated secrets, scoped API keys or OAuth, no raw card storage, tokenisation, least-privilege database access, HMAC verification on raw bodies, replay protection, rate limiting, PII redaction, audit logs, and PCI DSS controls.

## 12. Coding exercises to practise

### State transition validator

```python
def can_transition(graph, current, target):
    return target in graph.get(current, set())
```

Explain that the graph is explicit, so an invalid transition fails closed. Add tests for a legal move, a backward move, a terminal state, and an unknown state.

### Idempotent request handler

```python
async def process(key, body, store, charge):
    digest = sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    record = await store.lock_or_create(key, digest)
    if record.status == "COMPLETED":
        return record.response
    if record.request_hash != digest or record.status == "PROCESSING":
        raise ConflictError()
    try:
        result = await charge(body)
        await store.complete(key, result)
        return result
    except Exception:
        await store.fail(key)
        raise
```

Discuss the unique key, transaction boundary, request-hash mismatch, and the need for a shared durable store in a multi-instance deployment.

### Retry with exponential backoff

```python
for attempt in range(max_attempts):
    try:
        return await call()
    except RetryableError:
        if attempt == max_attempts - 1:
            raise
        await asyncio.sleep(base * 2**attempt + random.uniform(0, jitter))
```

Mention that business declines are not retryable, retries need a provider idempotency key, and exponential backoff prevents a retry storm.

### Weighted gateway selection

Be ready to normalise each factor to `[0, 1]`, filter unsupported or open gateways first, calculate the weighted sum, sort descending, and return both the winner and its score breakdown.

### HMAC verification

```python
expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
return hmac.compare_digest(received, expected)
```

Explain why the exact raw body matters and why ordinary `==` is avoided.

## 13. “Why Worldpay?” answer

> I’m interested in Worldpay because payment infrastructure combines software engineering with reliability, security, risk, and real business impact. This project made me interested in the same problems: routing transactions, handling gateway failure, reconciling state, and making retries safe. I would like to learn how those ideas are implemented at much larger scale across different markets and payment methods, and contribute with strong backend fundamentals, testing, and careful handling of money movement.

Do not claim knowledge of an internal Worldpay system unless it is in the job description or you have verified it.

## 14. Questions you can ask the interviewer

- Which part of the payment lifecycle would this role own most closely: acceptance, risk, settlement, reconciliation, or developer APIs?
- How do teams test provider integrations and failure scenarios safely?
- What are the most important reliability and observability metrics for this service?
- How are design reviews and production incident learnings shared?
- What would a successful first three months look like?

## 15. Study plan

Day 1: learn the 90-second story, repository map, and payment lifecycle.

Day 2: practise state transitions, idempotency, webhooks, and timeout ambiguity.

Day 3: revise Python async, FastAPI dependency injection, SQL joins/indexes/transactions, and pytest fixtures/mocks.

Day 4: practise the coding exercises above without looking at the solutions.

Day 5: explain the data model and design scaling/security improvements.

Final session: give a five-minute project walkthrough, answer the questions aloud, and prepare one real example of a debugging or teamwork situation from your own experience.

## 16. Facts to confirm before the interview

Be ready to say exactly what you personally implemented. Confirm your interview date, role/job description, preferred coding language, resume claims, and whether the interviewer expects a live demo. Those details determine which topics deserve the most practice.
