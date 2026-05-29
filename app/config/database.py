from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from app.config.settings import settings


def _engine_kwargs(url: str) -> dict:
    kwargs: dict = {
        "echo": False,
        "pool_pre_ping": True,
    }
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs.update(
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,
            pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
        )
    return kwargs


engine = create_async_engine(
    settings.DATABASE_URL,
    **_engine_kwargs(settings.DATABASE_URL),
)

read_engine = create_async_engine(
    settings.READ_DATABASE_URL or settings.DATABASE_URL,
    **_engine_kwargs(settings.READ_DATABASE_URL or settings.DATABASE_URL),
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

ReadOnlySessionLocal = async_sessionmaker(
    read_engine, class_=AsyncSession, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """Development/test helper only. Production uses Alembic migrations."""
    if not settings.AUTO_CREATE_TABLES:
        return
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    await engine.dispose()
    await read_engine.dispose()
