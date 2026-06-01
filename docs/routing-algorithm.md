# Routing Algorithm Specification

# Overview

The GatewayRouter service selects the optimal payment gateway based on historical performance metrics and real-time health indicators.

The routing system is designed to maximize:

* Transaction success rate
* System reliability
* Low latency
* Cost efficiency

---

# Routing Factors

## Success Rate

Historical percentage of successful transactions.

Higher success rate increases routing score.

---

## Latency

Average response time of gateway.

Lower latency increases routing score.

---

## Gateway Cost

Relative processing cost of gateway.

Lower cost increases routing preference.

---

## Circuit Breaker State

Possible states:

* CLOSED
* OPEN
* HALF_OPEN

Gateways in OPEN state are temporarily excluded.

---

# Scoring Formula

```text
Final Score =
    (Success Rate Weight × Success Rate)
  + (Latency Weight × Inverse Latency)
  + (Cost Weight × Inverse Cost)
```

---

# Default Weights

| Metric       | Weight |
| ------------ | ------ |
| Success Rate | 0.5    |
| Latency      | 0.3    |
| Cost         | 0.2    |

Weights are configurable via API.

---

# Failover Logic

If primary gateway fails:

1. Mark failure in health metrics
2. Recalculate scores
3. Select next-best gateway
4. Retry transaction

Failover target:

* Under 2 seconds

---

# Circuit Breaker

## CLOSED

Normal traffic flow.

## OPEN

Gateway temporarily blocked due to repeated failures.

## HALF_OPEN

Limited test traffic allowed to verify recovery.

---

# Health Metrics

Tracked metrics:

* Success rate
* Failure count
* Timeout count
* Average latency
* Consecutive failures

Sliding window methodology is used.

---

# Routing Flow

1. Receive payment request
2. Load gateway metrics
3. Exclude OPEN gateways
4. Calculate scores
5. Select highest-scoring gateway
6. Execute transaction
7. Update metrics

---

# Benefits

* Automatic failover
* Reduced downtime
* Improved transaction success
* Dynamic gateway optimization
* Better fault tolerance
