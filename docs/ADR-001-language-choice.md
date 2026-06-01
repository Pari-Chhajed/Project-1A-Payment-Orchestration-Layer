# ADR-001: Language and Framework Choice

# Status

Accepted

---

# Context

The project requires a scalable backend capable of handling:

* Async I/O
* Webhook processing
* Concurrent transactions
* REST APIs
* Database integrations
* High testability

---

# Decision

Python with FastAPI was selected.

PostgreSQL was selected as the primary database.

SQLAlchemy Async was selected as ORM.

Alembic was selected for database migrations.

---

# Rationale

## Python

Advantages:

* Rapid development
* Excellent async ecosystem
* Strong testing support
* Rich payment/backend libraries

## FastAPI

Advantages:

* Native async support
* Automatic OpenAPI generation
* High performance
* Strong validation support

## PostgreSQL

Advantages:

* ACID compliance
* Strong transactional guarantees
* Excellent concurrency handling

## SQLAlchemy Async

Advantages:

* Mature ORM
* Async database support
* Transaction management

## Alembic

Advantages:

* Version-controlled migrations
* Production-ready migration tooling

---

# Consequences

Benefits:

* Faster backend development
* Improved maintainability
* Better async scalability
* Easier testing

Tradeoffs:

* Async debugging complexity
* Higher learning curve for concurrency handling

---

# Final Outcome

The selected stack successfully supported:

* Gateway routing
* State machine transitions
* Webhook handling
* Idempotency protection
* Reconciliation engine
* Docker deployment
