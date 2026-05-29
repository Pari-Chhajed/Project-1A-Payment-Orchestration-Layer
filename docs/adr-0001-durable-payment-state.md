# ADR 0001: Durable Payment State and Attempt Lineage

Status: Accepted

Context: The assessment requires failover, retry lineage, reconciliation, and exactly-once style protections.

Decision: Keep `transactions` as the compatibility-facing payment aggregate and add `payment_attempts` plus `gateway_attempts` for retry and failover history.

Consequences:

- Existing API responses remain stable.
- Every gateway interaction can be compared during reconciliation.
- Failed attempts are auditable without overwriting the current payment state.
