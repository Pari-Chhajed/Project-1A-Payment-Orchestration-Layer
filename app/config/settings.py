from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://payflow:payflow@postgres:5432/payflow"
    API_KEY: str = "test-api-key-12345"
    RAZORPAY_WEBHOOK_SECRET: str = "razorpay_webhook_secret_test"
    STRIPE_WEBHOOK_SECRET: str = "stripe_webhook_secret_test"
    PAYU_WEBHOOK_SECRET: str = "payu_webhook_secret_test"
    UPI_WEBHOOK_SECRET: str = "upi_webhook_secret_test"
    LOG_LEVEL: str = "INFO"
    RECONCILIATION_INTERVAL_MINUTES: int = 15
    STALE_TRANSACTION_MINUTES: int = 5
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5
    CIRCUIT_BREAKER_TIMEOUT_SECONDS: int = 30
    GATEWAY_RATE_LIMIT_PER_SECOND: int = 25
    WEBHOOK_MAX_RETRIES: int = 3
    WEBHOOK_RETRY_BASE_SECONDS: int = 2
    UPI_COLLECT_TIMEOUT_MINUTES: int = 5

    model_config = {"env_file": ".env"}


settings = Settings()
