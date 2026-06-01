# PayFlow Commerce - Payment Orchestration Layer

## Overview

PayFlow Commerce is a scalable payment orchestration backend designed to simulate enterprise-grade payment processing systems. The platform provides unified transaction handling across multiple payment gateways while ensuring reliability, idempotency, reconciliation, routing intelligence, webhook handling, and failure recovery.

The system was developed as part of the Zetheta Engineering Internship Assessment.

---

# Key Features

* Multi-gateway payment orchestration
* Intelligent gateway routing
* Circuit breaker failover mechanism
* Transaction state machine
* Idempotency protection
* Webhook verification and deduplication
* Reconciliation engine
* Audit logging
* API key authentication
* Dockerized deployment
* Async processing architecture
* Comprehensive unit and E2E testing

---

# Technology Stack

| Component         | Technology       |
| ----------------- | ---------------- |
| Backend Framework | FastAPI          |
| Language          | Python 3.10      |
| Database          | PostgreSQL       |
| ORM               | SQLAlchemy Async |
| Migration Tool    | Alembic          |
| Testing           | Pytest           |
| Containerization  | Docker           |
| API Documentation | OpenAPI 3.0      |

---

# Project Structure

```text
app/
    api/
    core/
    db/
    models/
    services/
    gateways/

docs/
tests/
alembic/
docker/
```

---

# Supported Gateways

* Razorpay (Mock)
* Stripe (Mock)
* PayU (Mock)
* UPI (Mock)

Each gateway supports:

* Success simulation
* Timeout simulation
* 5xx error simulation
* Decline simulation
* Rate limiting simulation

---

# Transaction Lifecycle

Main transaction states:

* CREATED
* PENDING
* AUTHORIZED
* CAPTURED
* SETTLED
* FAILED
* REFUNDED
* CANCELLED

Detailed state transitions are documented in:

```text
docs/state-machine.md
```

---

# Intelligent Routing

Gateway selection is based on:

* Success rate
* Latency
* Cost
* Failure history
* Circuit breaker state

The router automatically performs failover to secondary gateways when failures occur.

---

# Idempotency

The system prevents duplicate payment processing using:

* Idempotency keys
* Database uniqueness guarantees
* Concurrent request protection
* Expiry-based cleanup

---

# Webhook Handling

Features include:

* Signature verification
* Deduplication
* Out-of-order event handling
* State machine integration
* Replay safety

---

# Reconciliation Engine

The reconciliation engine periodically compares internal transaction state with simulated gateway state to detect:

* Missing settlements
* Duplicate captures
* Amount mismatches
* Stuck transactions

---

# API Authentication

All protected APIs require:

```http
X-API-Key: your-api-key
```

---

# Running Locally

## Clone Repository

```bash
git clone <repository-url>
cd payflow-commerce
```

---

# Environment Setup

Create `.env` file:

```env
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/payflow
API_KEY=test-api-key
```

---

# Install Dependencies

```bash
pip install -r requirements.txt
```

---

# Run Database Migrations

```bash
alembic upgrade head
```

---

# Run Application

```bash
uvicorn app.main:app --reload
```

Application runs at:

```text
http://localhost:8000
```

---

# Docker Deployment

## Start Full System

```bash
docker-compose up --build
```

This starts:

* Application
* PostgreSQL
* Redis (if configured)

---

# Running Tests

## Unit Tests

```bash
pytest tests/unit/
```

## E2E Tests

```bash
pytest tests/e2e/
```

---

# API Documentation

OpenAPI docs available at:

```text
http://localhost:8000/docs
```

---

# Performance Considerations

The system includes:

* Async database access
* Sliding window metrics
* Failover routing
* Optimized transaction queries
* Connection pooling

---

# Security Measures

* API key authentication
* Webhook signature verification
* PII sanitization
* Request validation
* Input sanitization

---

# Documentation

Additional documentation:

* docs/architecture.md
* docs/state-machine.md
* docs/routing-algorithm.md
* docs/api-specification.yaml

---

# Test Coverage

The project includes:

* Unit tests
* Integration tests
* Failure scenario tests
* Webhook tests
* Routing tests
* State machine tests

Coverage target:

* 80%+ code coverage

---

# Author

Developed for Zetheta Internship Assessment.
