from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config.settings import Settings


class DatabaseManager:
    """Manages async SQLAlchemy engine and sessions (Supabase PgBouncer compatible)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Supabase Transaction Pooler (port 6543) runs PgBouncer in transaction mode,
        # which does not support asyncpg prepared statements -> statement_cache_size=0.
        connect_args: dict[str, object] = {}
        if "asyncpg" in settings.database_url:
            connect_args["statement_cache_size"] = 0

        self.engine: AsyncEngine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self.engine.dispose()
