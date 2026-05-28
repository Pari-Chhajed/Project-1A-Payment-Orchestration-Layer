"""
All database models in one file.
SQLite-compatible — no PostgreSQL-specific types needed.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import (
    String, Integer, BigInteger, Float, Boolean, Text,
    DateTime, ForeignKey, JSON, UniqueConstraint, Index
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.config.database import Base


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


# ─────────────────────────────────────────────────────────────────
# TRANSACTIONS
# ─────────────────────────────────────────────────────────────────
class Transaction(Base):
    __tablename__ = "transactions"

    id:                     Mapped[str]            = mapped_column(String(36), primary_key=True, default=new_uuid)
    merchant_id:            Mapped[str]            = mapped_column(String(100), nullable=False, default="payflow_default")
    merchant_order_id:      Mapped[str]            = mapped_column(String(255), nullable=False)
    state:                  Mapped[str]            = mapped_column(String(50), nullable=False, default="CREATED")
    gateway:                Mapped[Optional[str]]  = mapped_column(String(50))
    gateway_reference:      Mapped[Optional[str]]  = mapped_column(String(255))
    gateway_order_id:       Mapped[Optional[str]]  = mapped_column(String(255))
    payment_method:         Mapped[str]            = mapped_column(String(50), nullable=False)
    # Amount stored as integer paise — NEVER float
    amount_paise:           Mapped[int]            = mapped_column(BigInteger, nullable=False)
    currency:               Mapped[str]            = mapped_column(String(3), nullable=False, default="INR")
    captured_amount_paise:  Mapped[int]            = mapped_column(BigInteger, default=0)
    refunded_amount_paise:  Mapped[int]            = mapped_column(BigInteger, default=0)
    customer_id:            Mapped[Optional[str]]  = mapped_column(String(255))
    customer_email:         Mapped[Optional[str]]  = mapped_column(String(255))
    customer_phone:         Mapped[Optional[str]]  = mapped_column(String(20))
    description:            Mapped[Optional[str]]  = mapped_column(Text)
    meta:                   Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    trace_id:               Mapped[str]            = mapped_column(String(36), default=new_uuid)
    version:                Mapped[int]            = mapped_column(Integer, default=1)
    auth_hold_expires_at:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at:             Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at:             Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    state_logs:   Mapped[list["TransactionStateLog"]] = relationship("TransactionStateLog", back_populates="transaction", lazy="select")
    refunds:      Mapped[list["Refund"]]               = relationship("Refund", back_populates="transaction", lazy="select")

    __table_args__ = (
        UniqueConstraint("merchant_id", "merchant_order_id", name="uq_merchant_order"),
        Index("idx_txn_state", "state"),
        Index("idx_txn_gateway", "gateway"),
        Index("idx_txn_created", "created_at"),
        Index("idx_txn_gateway_ref", "gateway_reference"),
        Index("idx_txn_trace", "trace_id"),
    )


# ─────────────────────────────────────────────────────────────────
# IMMUTABLE AUDIT TRAIL
# ─────────────────────────────────────────────────────────────────
class TransactionStateLog(Base):
    __tablename__ = "transaction_state_log"

    id:               Mapped[str]            = mapped_column(String(36), primary_key=True, default=new_uuid)
    transaction_id:   Mapped[str]            = mapped_column(String(36), ForeignKey("transactions.id"), nullable=False)
    from_state:       Mapped[str]            = mapped_column(String(50), nullable=False)
    to_state:         Mapped[str]            = mapped_column(String(50), nullable=False)
    event:            Mapped[str]            = mapped_column(String(100), nullable=False)
    gateway_reference: Mapped[Optional[str]] = mapped_column(String(255))
    gateway_response: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    meta:             Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    trace_id:         Mapped[Optional[str]]  = mapped_column(String(36))
    created_at:       Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=now_utc)
    created_by:       Mapped[str]            = mapped_column(String(100), default="system")

    transaction: Mapped["Transaction"] = relationship("Transaction", back_populates="state_logs")

    __table_args__ = (
        Index("idx_log_txn", "transaction_id"),
        Index("idx_log_created", "created_at"),
    )


# ─────────────────────────────────────────────────────────────────
# IDEMPOTENCY KEYS
# ─────────────────────────────────────────────────────────────────
class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    merchant_id:    Mapped[str]            = mapped_column(String(100), primary_key=True, default="payflow_default")
    key:            Mapped[str]            = mapped_column(String(255), primary_key=True)
    request_hash:   Mapped[str]            = mapped_column(String(64), nullable=False)
    status:         Mapped[str]            = mapped_column(String(20), default="PROCESSING")
    response_code:  Mapped[Optional[int]]  = mapped_column(Integer)
    response_body:  Mapped[Optional[dict]] = mapped_column(JSON)
    transaction_id: Mapped[Optional[str]]  = mapped_column(String(36))
    created_at:     Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at:     Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)
    expires_at:     Mapped[datetime]       = mapped_column(DateTime(timezone=True))


# ─────────────────────────────────────────────────────────────────
# PROCESSED WEBHOOK EVENTS (deduplication)
# ─────────────────────────────────────────────────────────────────
class ProcessedWebhookEvent(Base):
    __tablename__ = "processed_webhook_events"

    gateway:        Mapped[str] = mapped_column(String(50), primary_key=True)
    event_id:       Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type:     Mapped[str] = mapped_column(String(100), nullable=False)
    payload_hash:   Mapped[str] = mapped_column(String(64), nullable=False)
    transaction_id: Mapped[Optional[str]] = mapped_column(String(36))
    processed_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


# ─────────────────────────────────────────────────────────────────
# WEBHOOK QUEUE (with DLQ)
# ─────────────────────────────────────────────────────────────────
class WebhookQueue(Base):
    __tablename__ = "webhook_queue"

    id:            Mapped[int]           = mapped_column(Integer, primary_key=True, autoincrement=True)
    gateway:       Mapped[str]           = mapped_column(String(50), nullable=False)
    event_id:      Mapped[str]           = mapped_column(String(255), nullable=False)
    payload:       Mapped[dict]          = mapped_column(JSON, nullable=False)
    raw_body:      Mapped[str]           = mapped_column(Text, nullable=False)
    signature:     Mapped[str]           = mapped_column(Text, nullable=False)
    status:        Mapped[str]           = mapped_column(String(20), default="PENDING")
    retry_count:   Mapped[int]           = mapped_column(Integer, default=0)
    max_retries:   Mapped[int]           = mapped_column(Integer, default=3)
    next_retry_at: Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=now_utc)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    created_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=now_utc)
    processed_at:  Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ─────────────────────────────────────────────────────────────────
# GATEWAY HEALTH METRICS
# ─────────────────────────────────────────────────────────────────
class GatewayHealthMetric(Base):
    __tablename__ = "gateway_health_metrics"

    id:              Mapped[int]   = mapped_column(Integer, primary_key=True, autoincrement=True)
    gateway:         Mapped[str]   = mapped_column(String(50), nullable=False)
    window_start:    Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_requests:  Mapped[int]   = mapped_column(Integer, default=0)
    success_count:   Mapped[int]   = mapped_column(Integer, default=0)
    failure_count:   Mapped[int]   = mapped_column(Integer, default=0)
    p95_latency_ms:  Mapped[int]   = mapped_column(Integer, default=400)
    created_at:      Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    __table_args__ = (Index("idx_health_gw_window", "gateway", "window_start"),)


# ─────────────────────────────────────────────────────────────────
# CIRCUIT BREAKER STATE
# ─────────────────────────────────────────────────────────────────
class CircuitBreakerState(Base):
    __tablename__ = "circuit_breaker_state"

    gateway:           Mapped[str] = mapped_column(String(50), primary_key=True)
    payment_method:    Mapped[str] = mapped_column(String(50), primary_key=True, default="all")
    state:             Mapped[str] = mapped_column(String(20), default="CLOSED")
    failure_count:     Mapped[int] = mapped_column(Integer, default=0)
    success_count:     Mapped[int] = mapped_column(Integer, default=0)
    failure_threshold: Mapped[int] = mapped_column(Integer, default=5)
    timeout_seconds:   Mapped[int] = mapped_column(Integer, default=30)
    last_failure_at:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_success_at:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    opened_at:         Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_at:        Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


# ─────────────────────────────────────────────────────────────────
# ROUTING CONFIG
# ─────────────────────────────────────────────────────────────────
class RoutingConfig(Base):
    __tablename__ = "routing_config"

    config_key:   Mapped[str]   = mapped_column(String(100), primary_key=True)
    config_value: Mapped[float] = mapped_column(Float, nullable=False)
    description:  Mapped[Optional[str]] = mapped_column(Text)
    updated_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


class GatewayRateLimitState(Base):
    __tablename__ = "gateway_rate_limit_state"

    gateway:           Mapped[str]      = mapped_column(String(50), primary_key=True)
    capacity:          Mapped[int]      = mapped_column(Integer, nullable=False)
    refill_per_second: Mapped[int]      = mapped_column(Integer, nullable=False)
    available_tokens:  Mapped[float]    = mapped_column(Float, nullable=False)
    updated_at:        Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)


# ─────────────────────────────────────────────────────────────────
# REFUNDS
# ─────────────────────────────────────────────────────────────────
class Refund(Base):
    __tablename__ = "refunds"

    id:               Mapped[str]            = mapped_column(String(36), primary_key=True, default=new_uuid)
    transaction_id:   Mapped[str]            = mapped_column(String(36), ForeignKey("transactions.id"), nullable=False)
    gateway_refund_id: Mapped[Optional[str]] = mapped_column(String(255))
    state:            Mapped[str]            = mapped_column(String(50), default="REFUND_INITIATED")
    amount_paise:     Mapped[int]            = mapped_column(BigInteger, nullable=False)
    currency:         Mapped[str]            = mapped_column(String(3), default="INR")
    reason:           Mapped[Optional[str]]  = mapped_column(Text)
    gateway_response: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at:       Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at:       Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    transaction: Mapped["Transaction"] = relationship("Transaction", back_populates="refunds")


# ─────────────────────────────────────────────────────────────────
# RECONCILIATION
# ─────────────────────────────────────────────────────────────────
class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"

    id:                    Mapped[str]           = mapped_column(String(36), primary_key=True, default=new_uuid)
    started_at:            Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=now_utc)
    completed_at:          Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status:                Mapped[str]           = mapped_column(String(20), default="RUNNING")
    transactions_checked:  Mapped[int]           = mapped_column(Integer, default=0)
    discrepancies_found:   Mapped[int]           = mapped_column(Integer, default=0)
    anomalies_found:       Mapped[int]           = mapped_column(Integer, default=0)
    error_message:         Mapped[Optional[str]] = mapped_column(Text)
    triggered_by:          Mapped[Optional[str]] = mapped_column(String(50), default="scheduled")


class ReconciliationLog(Base):
    __tablename__ = "reconciliation_log"

    id:               Mapped[str]            = mapped_column(String(36), primary_key=True, default=new_uuid)
    run_id:           Mapped[str]            = mapped_column(String(36), ForeignKey("reconciliation_runs.id"))
    transaction_id:   Mapped[str]            = mapped_column(String(36), ForeignKey("transactions.id"))
    discrepancy_type: Mapped[str]            = mapped_column(String(100), nullable=False)
    internal_state:   Mapped[Optional[str]]  = mapped_column(String(50))
    gateway_state:    Mapped[Optional[str]]  = mapped_column(String(100))
    resolution:       Mapped[Optional[str]]  = mapped_column(String(100))
    requires_manual:  Mapped[bool]           = mapped_column(Boolean, default=False)
    notes:            Mapped[Optional[str]]  = mapped_column(Text)
    created_at:       Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=now_utc)


# ─────────────────────────────────────────────────────────────────
# SECURITY AUDIT LOG
# ─────────────────────────────────────────────────────────────────
class SecurityAuditLog(Base):
    __tablename__ = "security_audit_log"

    id:           Mapped[str]            = mapped_column(String(36), primary_key=True, default=new_uuid)
    event_type:   Mapped[str]            = mapped_column(String(100), nullable=False)
    gateway:      Mapped[Optional[str]]  = mapped_column(String(50))
    source_ip:    Mapped[Optional[str]]  = mapped_column(String(45))
    payload_hash: Mapped[Optional[str]]  = mapped_column(String(64))
    details:      Mapped[Optional[dict]] = mapped_column(JSON)
    created_at:   Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=now_utc)


# ─────────────────────────────────────────────────────────────────
# GATEWAY ROUTES (routing decisions)
# ─────────────────────────────────────────────────────────────────
class GatewayRoute(Base):
    __tablename__ = "gateway_routes"

    id:             Mapped[str]            = mapped_column(String(36), primary_key=True, default=new_uuid)
    transaction_id: Mapped[str]            = mapped_column(String(36), ForeignKey("transactions.id"))
    gateway:        Mapped[str]            = mapped_column(String(50), nullable=False)
    score:          Mapped[float]          = mapped_column(Float, default=0.0)
    score_breakdown: Mapped[Optional[dict]] = mapped_column(JSON)
    selected:       Mapped[bool]           = mapped_column(Boolean, default=False)
    reason:         Mapped[Optional[str]]  = mapped_column(Text)
    created_at:     Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=now_utc)