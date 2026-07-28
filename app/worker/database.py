"""
Worker-specific database session factory.

Celery tasks run in separate processes and call asyncio.run() to execute async
code. Standard connection pools are NOT safe across multiple asyncio event
loops. NullPool disables pooling so every AsyncSession creates a fresh
connection in the current event loop and closes it immediately on exit.
"""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings

_worker_engine = create_async_engine(
    settings.DATABASE_URL,
    poolclass=NullPool,
    echo=False,
)

WorkerSession = async_sessionmaker(
    bind=_worker_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)
