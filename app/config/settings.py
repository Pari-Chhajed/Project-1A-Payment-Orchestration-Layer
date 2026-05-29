from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ENVIRONMENT: str = "development"
    SERVICE_ROLE: str = "api"
    DATABASE_URL: str = "sqlite+aiosqlite:///./payflow.db"
    READ_DATABASE_URL: str | None = None
    REDIS_URL: str = "redis://localhost:6379/0"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT_SECONDS: int = 10
    DB_POOL_RECYCLE_SECONDS: int = 1800
    AUTO_CREATE_TABLES: bool = False
    SEED_ON_STARTUP: bool = False
    START_SCHEDULER_IN_API: bool = False
    API_KEY: str = "test-api-key-12345"
    API_KEY_HASH: str | None = None
    DOCS_API_KEY: str | None = None
    RAZORPAY_WEBHOOK_SECRET: str = "razorpay_webhook_secret_test"
    STRIPE_WEBHOOK_SECRET: str = "stripe_webhook_secret_test"
    PAYU_WEBHOOK_SECRET: str = "payu_webhook_secret_test"
    UPI_WEBHOOK_SECRET: str = "upi_webhook_secret_test"
    CORS_ALLOWED_ORIGINS: str = "http://localhost:3000,http://localhost:8000"
    LOG_LEVEL: str = "INFO"
    JSON_LOGS: bool = True
    REQUEST_BODY_MAX_BYTES: int = 1_000_000
    RATE_LIMIT_PER_MINUTE: int = 600
    RECONCILIATION_INTERVAL_MINUTES: int = 15
    STALE_TRANSACTION_MINUTES: int = 5
    WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS: int = 300
    WEBHOOK_INLINE_PROCESSING: bool = True
    QUEUE_MAX_RETRIES: int = 3
    QUEUE_BASE_DELAY_SECONDS: int = 1
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5
    CIRCUIT_BREAKER_TIMEOUT_SECONDS: int = 30
    OTEL_ENABLED: bool = False
    PROMETHEUS_ENABLED: bool = True

    model_config = {"env_file": ".env"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
