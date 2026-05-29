from app.config.settings import settings

try:
    from redis.asyncio import Redis
except Exception:  # pragma: no cover - local lightweight test environments may skip Redis.
    Redis = None  # type: ignore

_client: Redis | None = None


class _NoopRedis:
    async def hset(self, *args, **kwargs):
        return None

    async def aclose(self):
        return None


def get_redis():
    global _client
    if Redis is None:
        return _NoopRedis()
    if _client is None:
        _client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
