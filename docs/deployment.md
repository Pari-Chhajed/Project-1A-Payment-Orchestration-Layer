# Deployment

Run the full stack:

```bash
docker compose up --build
```

The compose stack starts PostgreSQL, Redis, PgBouncer, an Alembic migration job, the API service, the webhook worker, and the scheduler.

Useful endpoints:

- `GET /api/v1/live`
- `GET /api/v1/ready`
- `GET /api/v1/health`
- `GET /metrics`

Production notes:

- Set `ENVIRONMENT=production` to disable Swagger/Redoc.
- Rotate `API_KEY` and webhook secrets outside source control.
- Use PgBouncer transaction pooling through `DATABASE_URL`.
- Keep `WEBHOOK_INLINE_PROCESSING=false` when worker service is enabled.
