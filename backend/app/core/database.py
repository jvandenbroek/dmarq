import logging
import os
from typing import AsyncGenerator
from urllib.parse import urlparse, urlunparse

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_ASYNC_TO_SYNC_SCHEMES = {
    "postgresql+asyncpg": "postgresql+psycopg2",
}


def _make_sync_db_url(url: str) -> str:
    """Return the synchronous-driver equivalent of *url*.

    Kubernetes and docker-compose deployments sometimes configure DATABASE_URL
    with an async driver scheme (e.g. ``postgresql+asyncpg://``).  Alembic and
    the synchronous SQLAlchemy engine used here require a sync driver, so we
    map known async schemes to their psycopg2 equivalents.

    Only the scheme component of the URL is rewritten; all other parts
    (credentials, host, path, query) are left untouched.
    """
    parsed = urlparse(url)
    sync_scheme = _ASYNC_TO_SYNC_SCHEMES.get(parsed.scheme)
    if sync_scheme is None:
        return url
    return urlunparse(parsed._replace(scheme=sync_scheme))


def _ensure_sqlite_dir(url: str) -> None:
    """Create the parent directory for a SQLite database file if needed.

    For SQLite URLs (``sqlite:///relative/path`` or ``sqlite:////absolute/path``),
    the parent directory must exist before SQLAlchemy tries to open (or create)
    the file.  This is a no-op for in-memory databases (``sqlite://``) and for
    non-SQLite URLs.
    """
    sa_url = make_url(url)
    if not sa_url.drivername.startswith("sqlite"):
        return
    db_path = sa_url.database
    if not db_path or db_path == ":memory:":
        return  # in-memory – nothing to create
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)


settings = get_settings()

_sync_url = _make_sync_db_url(settings.DATABASE_URL)

# Ensure the parent directory exists before SQLAlchemy tries to open the file
_ensure_sqlite_dir(_sync_url)

def _engine_connect_args(url: str) -> dict:
    """Driver-specific connect arguments for the synchronous engine.

    On PostgreSQL, ``DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS`` (opt-in, disabled
    by default) arms ``idle_in_transaction_session_timeout`` so a connection
    that somehow ends up abandoned with an open transaction cannot pin a
    backend (and its locks/xmin horizon) indefinitely.  It is a backstop, not
    the mechanism for releasing sessions - pick a value well above the slowest
    legitimate request before enabling it.
    """
    sa_url = make_url(url)
    if not sa_url.drivername.startswith("postgresql"):
        return {}
    timeout_seconds = int(
        getattr(settings, "DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS", 0) or 0
    )
    if timeout_seconds <= 0:
        return {}
    return {
        "options": f"-c idle_in_transaction_session_timeout={timeout_seconds * 1000}"
    }


# Configure SQLAlchemy (normalise async driver schemes to their sync equivalents).
# Pool sizing is skipped for SQLite (StaticPool/NullPool there; pool_size and
# max_overflow are invalid kwargs for SQLite's default pool implementation).
_engine_kwargs = {
    "pool_pre_ping": True,
    "connect_args": _engine_connect_args(_sync_url),
}
if not make_url(_sync_url).drivername.startswith("sqlite"):
    _engine_kwargs["pool_size"] = settings.DB_POOL_SIZE
    _engine_kwargs["max_overflow"] = settings.DB_MAX_OVERFLOW

engine = create_engine(_sync_url, **_engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create base class for SQLAlchemy models
Base = declarative_base()


async def get_db() -> AsyncGenerator[Session, None]:
    """Dependency that yields a request-scoped SQLAlchemy session.

    This is deliberately an ``async`` generator, and should stay one.

    FastAPI runs a **synchronous** generator dependency through
    ``fastapi.concurrency.contextmanager_in_threadpool``, which only invokes the
    generator's ``__exit__`` from its ``except Exception`` / ``else`` branches.
    ``asyncio.CancelledError`` derives from ``BaseException``, so if a request
    task is ever cancelled the cancellation escapes that helper without driving
    the generator's ``finally``.  The session is then merely *abandoned*: it is
    closed only if and when the garbage collector finalises the orphaned
    generator, and until that happens the pooled DBAPI connection stays checked
    out with an open transaction (PostgreSQL: ``idle in transaction``).  This
    was verified against the pinned FastAPI/Starlette/anyio versions with a
    minimal app: teardown of a sync generator dependency ran only via
    ``GeneratorExit`` from the collector, never from the cancellation itself.

    An ``async`` generator dependency is driven by ``AsyncExitStack``/
    ``contextlib.asynccontextmanager`` instead, which throws the
    ``CancelledError`` *into* the generator, so the ``finally`` below always
    runs on the cancellation path - no reliance on the garbage collector.
    ``Session.close()`` releases the connection back to the pool and rolls back
    any open transaction; it is cheap enough to run on the event loop (this app
    already performs all of its ORM work there).
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001 - never mask the original failure
            logger.exception("Failed to close request database session")

