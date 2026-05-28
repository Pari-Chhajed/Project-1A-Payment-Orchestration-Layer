from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite+aiosqlite:///./payflow.db"
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

    model_config = {"env_file": ".env"}


settings = Settings()