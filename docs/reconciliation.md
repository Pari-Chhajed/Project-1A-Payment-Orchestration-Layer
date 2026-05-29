# Reconciliation

The reconciliation engine scans stale in-flight payments, polls mock gateway status, compares gateway truth with internal state, and writes `reconciliation_runs` plus `reconciliation_log` records.

```mermaid
flowchart TD
  Start[Scheduled or manual trigger] --> Claim[Claim stale transactions with SKIP LOCKED]
  Claim --> Poll[Poll gateway status API]
  Poll --> Compare{State matches?}
  Compare -->|yes| Done[No-op]
  Compare -->|recoverable| Auto[RECONCILIATION_OVERRIDE]
  Compare -->|critical| Review[RECONCILIATION_REQUIRED / MANUAL_REVIEW]
  Auto --> Audit[Audit log]
  Review --> Audit
```

Critical settlement discrepancies are never auto-refunded. They are flagged for human review so the simulation preserves financial consistency.
