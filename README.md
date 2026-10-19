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

## 17. The complete beginner story: what happens when a customer pays

Imagine a customer is buying a product for ₹1,000. The customer clicks **Pay**. The browser sends a request to this system. The system is not the bank and does not directly move the money. It is the coordinator between the merchant and a payment gateway.

The browser sends:

```text
POST /api/v1/payments
X-API-Key: test-api-key-12345
Idempotency-Key: order-attempt-abc-123
Content-Type: application/json
```

```json
{
  "merchant_order_id": "ORDER-1001",
  "amount": 100000,
  "currency": "INR",
  "payment_method": "card",
  "customer": {
    "id": "CUST-1",
    "email": "customer@example.com",
    "phone": "9999999999"
  }
}
```

`100000` means 100,000 paise, or ₹1,000.00. The request does not contain raw card details in this project. In a real payment system, card details would normally be collected by a secure hosted checkout or tokenised provider component.

### Step 1: the request enters FastAPI

`app/main.py` creates the FastAPI application and includes all routers. Before a route function runs, middleware runs.

`request_logger_middleware` creates a request ID, measures duration, logs the method, path, status, and duration, and returns the ID in the `X-Request-ID` response header.

`api_key_middleware` checks `X-API-Key`. Health and webhook paths are excluded because gateway webhooks need to be authenticated using their signatures instead of the merchant API key. A missing or incorrect API key returns HTTP 401 immediately, so the payment route is never reached.

### Step 2: FastAPI validates the body and injects the database session

`app/routes/payments.py` defines Pydantic models:

- `CustomerSchema` requires customer ID, email, and phone.
- `CreatePaymentRequest` requires merchant order ID, integer amount, currency, payment method, and customer.
- `CaptureRequest` optionally contains a capture amount.
- `RefundRequest` contains a refund amount and optional reason.

FastAPI converts valid JSON into these Python objects. It also calls the `get_db` dependency from `app/config/database.py`. `get_db` opens an async SQLAlchemy session, yields it to the route, and closes it afterward.

The route checks the `Idempotency-Key` header. This header is mandatory for payment creation because a client may retry the same HTTP request when it does not receive a response.

### Step 3: the payment service starts the transaction

The route calls `payment_service.create_payment`. The route is intentionally thin: it translates HTTP into a service call and translates service exceptions into HTTP responses. Business logic stays in the service layer.

`create_payment` performs these actions in order:

1. Generates a trace ID so logs and state transitions can be correlated.
2. Calls `validate_amount` to reject zero, negative, non-integer, or excessively large amounts.
3. Calls `idempotency.check_and_lock`.
4. Creates a `Transaction` row in state `CREATED`.
5. Selects a gateway.
6. Calls the gateway through the adapter map.
7. Updates the state and writes audit logs.
8. Commits the transaction and caches the response against the idempotency key.

### Step 4: idempotency prevents a double charge

Suppose the customer clicks Pay twice or the browser retries because the network is slow. Both requests carry the same key.

`check_and_lock` creates an SHA-256 hash of important request fields: merchant order ID, amount, and payment method. It stores the key together with the hash and status `PROCESSING`.

There are four important outcomes:

- No existing row: create a new `PROCESSING` row and continue.
- Existing `PROCESSING` row: return a conflict because another request is already working on it.
- Existing `COMPLETED` row: return the saved response and do not contact a gateway again.
- Existing row with a different request hash: reject it because one idempotency key cannot represent two different payments.

After successful payment creation, `mark_completed` stores the response body, HTTP status, transaction ID, and `COMPLETED` status. A scheduled cleanup later removes expired keys.

This is the difference between **retrying an HTTP request** and **creating a second payment**. The key makes a retry safe.

### Step 5: the router chooses a gateway

The payment service calls `gateway_router.select_gateway`. This is the decision point. The payment service does not contain separate `if gateway == ...` business rules for every provider.

The router loops through the `GatewayName` enum: Razorpay, Stripe, PayU, and UPI. For every gateway it:

1. Asks the circuit breaker whether the gateway is allowed.
2. Checks whether the gateway supports the requested payment method using `GATEWAY_METHODS`.
3. Reads recent success and latency metrics.
4. Calculates an estimated gateway cost.
5. Converts each factor to a score from 0 to 1.
6. Applies the configured weights.
7. Stores a `GatewayScore` object containing both component scores and raw values.

For example, if a card payment is requested, UPI is filtered out because it only supports UPI. If Stripe has a high success rate and low latency but costs more, the weighted score decides whether the reliability benefit is worth the extra cost.

The router returns a `RouteDecision` containing the selected gateway, all gateway scores, and a human-readable reason. The payment service writes this decision to `gateway_routes`. That is why the system can later answer, “Why did this payment use Stripe?”

### Step 6: the payment state changes to `ROUTE_SELECTED`

The payment service sets `txn.gateway` and calls `state_machine.transition`:

```text
CREATED -> ROUTE_SELECTED
```

The state machine reads the transaction, checks the transition graph, increments the version, updates the state, and inserts a `TransactionStateLog`. The log records the old state, new state, event, gateway reference, metadata, trace ID, and actor.

The state machine is the guardrail. Code cannot directly change a transaction from `CREATED` to `REFUNDED` through the normal service path.

### Step 7: the adapter is called

The payment service calls `_call_gateway_with_failover`. It changes the transaction to `AUTH_INITIATED` and looks up the provider function from `AUTHORISE_FN`:

```python
AUTHORISE_FN = {
    GatewayName.RAZORPAY: razorpay_authorise,
    GatewayName.STRIPE: stripe_authorise,
    GatewayName.PAYU: payu_authorise,
    GatewayName.UPI: upi_authorise,
}
```

This map is the exact place where the selected gateway becomes a function call. The route is selected once for the attempt; the adapter map dispatches that attempt to the correct provider implementation.

The service passes the same common information to every adapter: transaction/order ID, amount in paise, currency, payment method, idempotency key, and test controls. Each adapter translates that common information into the provider-shaped response.

The adapters return a common `GatewayResponse` object:

- `success`: whether the provider call succeeded;
- `gateway_order_id`: provider order or intent ID;
- `gateway_payment_id`: provider payment ID;
- `status`: provider status;
- `amount_paise` and `currency`;
- `error_code` and `error_message`;
- `raw_response` for audit/debug information;
- `latency_ms`.

The payment service therefore does not need to understand every provider response format.

### Step 8: authorisation and capture are different

For Razorpay, Stripe, and PayU in this design, authorisation means the provider accepted or reserved the payment. The card may be authorised but not yet captured. The state becomes:

```text
AUTH_INITIATED -> AUTHORISED
```

Later, the merchant calls:

```text
POST /api/v1/payments/{payment_id}/capture
```

The service selects the capture function from `CAPTURE_FN`, changes the state to `CAPTURE_INITIATED`, and tries up to three times with 1-second and 2-second backoff. Success produces `CAPTURED` or `PARTIALLY_CAPTURED`.

UPI is handled differently in this implementation. The UPI adapter represents an immediate payment flow, so the successful authorisation path moves directly to `CAPTURED` and records the captured amount.

The important conceptual difference is:

- **Authorisation:** the gateway approves or reserves funds.
- **Capture:** the merchant asks the gateway to take the authorised funds.
- **Settlement:** the financial systems later transfer and reconcile funds between institutions.

### Step 9: errors and failover

The adapter call is wrapped in `asyncio.wait_for`, so a gateway cannot hold the request forever.

If the adapter returns a retryable server error, rate-limit error, or connection failure, the service:

1. Records a circuit-breaker failure.
2. Records a failed health metric and latency.
3. Moves the transaction to `AUTH_FAILED` or `AUTH_TIMEOUT`.
4. Asks the router for another gateway, excluding gateways already tried.
5. Changes the transaction back to `ROUTE_SELECTED` with failover metadata.
6. Tries the next adapter.

A payment decline is different from an infrastructure failure. Insufficient funds is a business result and normally should not be retried against another gateway. A timeout is especially dangerous because the gateway may have charged the customer even though the response was lost. A production system should query the original gateway using its provider idempotency key before failing over.

### Step 10: completion and response

On successful authorisation, the service stores the gateway payment ID and order ID, records a seven-day authorisation hold expiry, updates gateway health, commits the transaction, and converts it into a response dictionary.

The response includes:

```json
{
  "id": "internal-transaction-id",
  "merchant_order_id": "ORDER-1001",
  "state": "AUTHORISED",
  "gateway": "stripe",
  "gateway_reference": "pi_...",
  "amount": "1000.00",
  "amount_paise": 100000,
  "currency": "INR",
  "captured_amount": "0.00",
  "refunded_amount": "0.00",
  "payment_method": "card",
  "trace_id": "..."
}
```

The second request with the same idempotency key receives the cached response instead of creating another transaction.

## 18. What exactly is a gateway adapter?

A gateway is an external payment provider. Razorpay, Stripe, PayU, and UPI do not use identical APIs or status names. One might call a payment `succeeded`, another `captured`, and another `success`.

An adapter is a small translation layer. The application speaks a common internal language, and each adapter speaks the provider language.

### Common operations in this repository

| Operation | Meaning | Adapter map/function |
|---|---|---|
| Authorise | Start or approve a payment | `AUTHORISE_FN`, `*_authorise` |
| Capture | Take authorised funds | `CAPTURE_FN`, `*_capture` |
| Refund | Return captured funds | `REFUND_FN`, `*_refund` |
| Void | Cancel an uncaptured authorisation | `VOID_FN`, `*_void` |
| Fetch status | Ask provider for the current status | `FETCH_FN`, `*_fetch_status` |

Razorpay and Stripe have separate capture/refund/void functions. PayU is modelled as a success-style flow and has simplified capture/void behavior. UPI does not support separate capture in this implementation, and its refund function returns `NOT_SUPPORTED` because the project models that process as an external NPCI operation.

`_mock` is the testing simulator. It can add delay, sleep until a timeout, return a server error, return a decline, return a rate-limit response, or raise a connection error. The `X-Mock-Response` header selects those behaviors during local tests.

In a real system, the adapter functions would use an HTTP client or provider SDK, include authentication and provider idempotency headers, map provider error codes, enforce connection/read timeouts, and never expose sensitive raw responses to clients.

## 19. Webhooks explained from zero

A webhook is an HTTP request sent by the gateway to our server. It is the gateway saying, “Something about this payment changed.”

For example, the merchant API may return `AUTHORISED`, and several seconds later Stripe may send `payment_intent.succeeded`. Or the customer may complete a UPI payment in another app while the original browser request is still waiting.

The webhook endpoint is not the same as the payment creation route:

- The merchant calls `POST /api/v1/payments` to ask for a payment.
- The gateway calls `POST /api/v1/webhooks/{gateway}` to report a later event.

### Webhook processing sequence

1. FastAPI reads the exact raw request body.
2. The route reads the provider signature header.
3. The webhook service verifies the signature using the configured secret.
4. It parses the provider event and extracts an event ID, payment ID, order ID, event type, and amount.
5. It checks whether `(gateway, event_id)` already exists in `processed_webhook_events`.
6. It finds the internal transaction using the gateway reference or order ID.
7. It checks that the webhook amount matches the transaction amount.
8. It maps the provider event to a canonical `TransactionState`.
9. It applies the transition only if it is legal.
10. It inserts the processed-event record and commits.

### Why the raw body is used

The provider signs the exact bytes it sent. If the server parses JSON and serialises it again before checking the signature, whitespace, field order, or escaping may change and the signature may no longer match. Therefore the route reads `await request.body()` first and passes that exact string to the verifier.

### Signature functions

- `verify_razorpay_signature`: HMAC-SHA256 of the raw body.
- `verify_stripe_signature`: signs `timestamp.raw_body` and rejects timestamps older than five minutes.
- `verify_payu_signature`: computes the provider-specific SHA-512 field sequence.
- `verify_upi_signature`: HMAC-SHA256 of the raw body.
- `hash_payload`: stores a SHA-256 payload hash for audit and comparison.

`hmac.compare_digest` is used instead of ordinary string equality to reduce timing-attack leakage.

### Duplicate webhook example

A gateway may retry the same event because our response was lost. The first delivery inserts:

```text
(gateway="stripe", event_id="evt_123")
```

The second delivery finds that row and returns `duplicate: true`. It does not apply the state transition again.

### Out-of-order webhook example

Suppose the database is already `CAPTURED`, but a late `payment.authorized` event arrives. The state machine says that moving backward is invalid. The service logs a warning and ignores the transition. That protects the current state from an old message.

The current implementation processes webhooks synchronously. The `WebhookQueue` table shows the intended future design for durable retries and a dead-letter queue, but it is not currently wired into the routes.

## 20. Reconciliation explained from zero

Reconciliation is a comparison between two records of reality:

1. What our database believes happened.
2. What the external gateway says happened.

A payment system cannot assume that every API response and webhook will arrive perfectly. A process can crash after the gateway captures a payment but before our database is updated. A webhook can be lost, delayed, duplicated, or malformed. Reconciliation repairs or flags these differences.

### Scheduled reconciliation flow

`app/workers/scheduler.py` starts an APScheduler job. By default, reconciliation runs every 15 minutes.

`reconciliation_service.run`:

1. Creates a `ReconciliationRun` with status `RUNNING`.
2. Finds stale transactions in `AUTH_INITIATED`, `CAPTURE_INITIATED`, or `REFUND_INITIATED`.
3. Restricts the batch to transactions with a gateway and gateway reference.
4. Calls `_reconcile_one` for each transaction.
5. Updates counts for checked transactions, discrepancies, and critical anomalies.
6. Commits a `COMPLETED` run, or records `FAILED` with an error message.

`_reconcile_one` chooses a `FETCH_FN` adapter and asks the gateway for the current payment status. `GATEWAY_STATUS_MAP` converts provider words such as `succeeded`, `success`, `captured`, `refunded`, `failed`, `pending`, and `expired` into canonical states.

If the internal and gateway states match, nothing is recorded. If they differ:

- A normal mismatch is written to `reconciliation_log` and automatically corrected when the canonical state machine allows it.
- A critical mismatch, such as our database saying `CAPTURED` while the gateway says `FAILED`, is marked `CRITICAL_ANOMALY` and `MANUAL_REVIEW_REQUIRED`.
- If the desired correction itself is illegal, the transaction moves to `RECONCILIATION_MISMATCH` for investigation.

Reconciliation is therefore not the same as a retry. A retry repeats an operation. Reconciliation first asks what actually happened and then brings internal records into line safely.

### Reconciliation APIs

- `POST /api/v1/reconciliation/trigger`: manually starts a run.
- `GET /api/v1/reconciliation/reports`: lists recent runs.
- `GET /api/v1/reconciliation/reports/{run_id}`: returns one run and its discrepancy details.

## 21. Every public API in this project

All non-health and non-webhook APIs require `X-API-Key` in the current implementation.

### Payment APIs

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/payments` | Create and authorise a payment; requires `Idempotency-Key` |
| `GET` | `/api/v1/payments/{payment_id}` | Return one transaction |
| `GET` | `/api/v1/payments` | List up to 50 payments, optionally filtered by order ID or state |
| `POST` | `/api/v1/payments/{payment_id}/capture` | Capture all or part of an authorised payment |
| `POST` | `/api/v1/payments/{payment_id}/void` | Void an authorisation |
| `POST` | `/api/v1/payments/{payment_id}/refund` | Refund all or part of a refundable payment |
| `GET` | `/api/v1/payments/{payment_id}/refunds` | List refund records |
| `GET` | `/api/v1/payments/{payment_id}/timeline` | Return the audit timeline |

### Webhook APIs

| Method | Path | Provider behavior |
|---|---|---|
| `POST` | `/api/v1/webhooks/razorpay` | JSON body plus `X-Razorpay-Signature` |
| `POST` | `/api/v1/webhooks/stripe` | JSON body plus `Stripe-Signature` |
| `POST` | `/api/v1/webhooks/payu` | Form-encoded body and PayU hash |
| `POST` | `/api/v1/webhooks/upi` | JSON body plus `X-UPI-Signature` |

### Gateway and routing APIs

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/gateways` | List circuit-breaker states |
| `GET` | `/api/v1/gateways/{name}/health` | Return state, health score, failures, success rate, and latency |
| `GET` | `/api/v1/gateways/{name}/metrics` | Return recent gateway metric windows |
| `PUT` | `/api/v1/gateways/{name}/config` | Update failure threshold or timeout |
| `POST` | `/api/v1/gateways/{name}/circuit-breaker/reset` | Manually close a breaker |
| `GET` | `/api/v1/routing/config` | Return routing weights |
| `PUT` | `/api/v1/routing/config` | Update weights; they must sum to 1.0 |

### Reporting and health APIs

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/analytics/success-rate` | Aggregate gateway success and latency by period |
| `GET` | `/api/v1/analytics/volume` | Count transactions, captured volume, and success rate |
| `GET` | `/api/v1/health` | Check database and gateway health |
| `GET` | `/` | Return API, docs, and health links |

## 22. Function-by-function map for interviews

You do not need to memorise every line. Know each function's responsibility and the order in which the important functions call one another.

### Application and database

- `lifespan`: initialises tables, seeds data, starts workers, and shuts workers down.
- `api_key_middleware`: protects merchant APIs.
- `request_logger_middleware`: adds request IDs and timing.
- `global_exception_handler`: hides unexpected internal details from the client.
- `get_db`: yields an async SQLAlchemy session.
- `init_db`: creates tables from SQLAlchemy metadata.

### Payment service

- `_get_txn`: loads one transaction or raises not found.
- `_txn_to_dict`: converts a database object into an API-safe response.
- `create_payment`: coordinates validation, idempotency, routing, gateway call, commit, and response caching.
- `_call_gateway_with_failover`: performs authorisation with timeout, metrics, circuit-breaker updates, state changes, and failover.
- `_attempt_failover`: chooses the next gateway and records the failover route.
- `capture_payment`: captures with up to three attempts and supports partial capture states.
- `refund_payment`: checks refundable state and balance, calls the refund adapter, updates refund records, and transitions the transaction.
- `void_payment`: calls the void adapter and transitions to `VOIDED`.
- `get_transaction`: returns one payment.
- `get_transaction_by_order_id`: finds a merchant payment by order ID.
- `list_refunds`: returns refund records for a payment.

### Router and reliability

- `_get_weights`: loads persisted weights with defaults.
- `_get_metrics`: calculates recent success rate and average p95 latency.
- `select_gateway`: filters and scores gateways.
- `get_failover_gateway`: returns the highest-ranked gateway not already tried.
- `record_metric`: writes one health metric window.
- `update_weights`: validates that routing weights sum to 1.0 and persists them.
- `_get_or_create`: loads or creates breaker state.
- `is_allowed`: implements CLOSED, OPEN, and HALF_OPEN decisions.
- `record_success` and `record_failure`: update breaker counters and timestamps.
- `get_health_score`: converts breaker state into a score.
- `reset`: manually returns a breaker to CLOSED.

### State and webhooks

- `is_valid_transition`: checks the transition graph.
- `get_valid_next_states`: returns legal next states for an error response or UI.
- `_sanitize_gateway_response`: redacts selected sensitive fields.
- `transition`: locks a transaction, validates and applies a state change, and writes the audit event.
- `get_timeline`: reads the ordered audit trail.
- `_process`: shared webhook pipeline after provider-specific parsing.
- `_extract_amount`: extracts and normalises the provider amount.
- `process_razorpay`, `process_stripe`, `process_payu`, `process_upi`: verify and parse each provider's webhook format.

### Reconciliation and workers

- `run`: creates a reconciliation run and processes stale transactions.
- `_reconcile_one`: fetches provider status, maps it, logs mismatches, and attempts safe correction.
- `get_report`: returns a run and its logs.
- `_run_reconciliation`: scheduled wrapper around `run`.
- `_expire_upi_transactions`: marks old UPI collect requests as expired.
- `_cleanup_idempotency_keys`: deletes expired idempotency rows.
- `start_scheduler` and `stop_scheduler`: manage APScheduler.

## 23. A spoken walkthrough you can practise

> A customer sends a create-payment request. FastAPI first authenticates the merchant API key, creates a request ID, validates the Pydantic body, and injects an async database session. The payment route requires an idempotency key and calls the payment service. The service validates the integer paise amount and stores a request hash so retries cannot create another charge. It creates a transaction in `CREATED`, asks the gateway router to score eligible gateways, stores the decision, and moves the transaction to `ROUTE_SELECTED`. The service then changes it to `AUTH_INITIATED` and dispatches to the selected adapter through `AUTHORISE_FN`. The adapter returns a common `GatewayResponse`, so provider-specific response formats stay isolated. On success, a card-like flow becomes `AUTHORISED`; a later capture changes it to `CAPTURED`. A UPI success is captured immediately in this implementation. On retryable errors or timeouts, the circuit breaker and health metrics are updated and the router can choose another gateway. Every state change is recorded in `transaction_state_log`. Later, a provider webhook is verified using the raw body and signature, deduplicated, amount-checked, mapped to our canonical state, and safely applied. Scheduled reconciliation asks gateways for status on stale transactions and corrects or flags mismatches. This gives the merchant one API while handling provider differences and payment failure cases in one place.

That is the story. If the interviewer interrupts, stop at the part they want and go deeper there.

## 24. Beginner glossary

- **Merchant:** the business accepting payment.
- **Customer:** the person paying the merchant.
- **Gateway/PSP:** an external payment provider that talks to banks and payment networks.
- **Authorisation:** approval or reservation of funds.
- **Capture:** instruction to take authorised funds.
- **Refund:** return of captured funds.
- **Void:** cancel an authorisation before capture.
- **Settlement:** later financial transfer and accounting between institutions.
- **Webhook:** provider-to-server event notification.
- **Idempotency:** repeating the same request produces one logical result.
- **Failover:** trying another eligible provider after a failure.
- **Circuit breaker:** temporarily stopping calls to an unhealthy provider.
- **Reconciliation:** comparing internal records with provider records.
- **DLQ:** dead-letter queue for messages that exceeded retry attempts.
- **HMAC:** keyed hash used to verify that a message came from a trusted sender and was not modified.
- **Trace ID:** identifier used to follow one transaction through logs and services.
- **Paise:** integer subunit of INR; 100 paise equals ₹1.

