import os
from contextlib import contextmanager, asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import AsyncIterator, Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker


# Load .env file if it exists
_env_file = Path(__file__).resolve().parents[2] / ".env"
if _env_file.exists():
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"\'')
                if key and key not in os.environ:
                    os.environ[key] = value


def database_url() -> str:
    return (
        f"postgresql+psycopg://{os.environ['POSTGRES_USER']}:"
        f"{os.environ['POSTGRES_PASSWORD']}@{os.environ.get('POSTGRES_HOST', 'postgres')}:"
        f"{os.environ.get('POSTGRES_PORT', '5432')}/{os.environ['POSTGRES_DB']}"
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return create_engine(database_url(), pool_pre_ping=True, future=True)


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


@contextmanager
def get_session() -> Iterator[Session]:
    session = _session_factory()()
    try:
        yield session
    finally:
        session.close()


# Async engine/factory — the Knowledge Agent's retrieval nodes require an
# async_sessionmaker (per agent/knowledge/graph.py): sessions are opened
# per-call so the hybrid strategy's parallel fan-out never shares a single
# AsyncSession across coroutines. Reuses the same POSTGRES_* config.
@lru_cache(maxsize=1)
def get_async_engine() -> AsyncEngine:
    return create_async_engine(database_url(), pool_pre_ping=True, future=True)


@lru_cache(maxsize=1)
def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_async_engine(), autoflush=False, expire_on_commit=False
    )


@asynccontextmanager
async def get_async_session() -> AsyncIterator[AsyncSession]:
    actual_factory = get_async_session_factory()
    async with actual_factory() as session:
        try:
            yield session
        finally:
            await session.close()
