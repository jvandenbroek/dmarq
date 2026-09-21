"""Regression tests for request-scoped DB session teardown under cancellation.

Background
----------
FastAPI runs *synchronous* generator dependencies through
``fastapi.concurrency.contextmanager_in_threadpool``, which only calls the
generator's ``__exit__`` from its ``except Exception`` and ``else`` branches.
``asyncio.CancelledError`` derives from ``BaseException``, so whenever a request
task is cancelled the generator is never driven to completion - it is merely
abandoned, and its ``finally`` runs only if/when the garbage collector finalises
it.  For a DB-session dependency that means the SQLAlchemy session, and the
pooled DBAPI connection it holds, can stay checked out with an open transaction
(PostgreSQL: ``idle in transaction``) for an unbounded amount of time.

``get_db`` is therefore an *async* generator: async generator dependencies are
driven by ``AsyncExitStack``/``contextlib.asynccontextmanager``, which throws
the ``CancelledError`` into the generator so the ``finally`` always runs.
"""

import gc
import inspect

import anyio
import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import engine, get_db


def test_get_db_is_async_generator_dependency():
    """Guard the fix itself.

    If this ever becomes a plain ``def`` generator again, FastAPI will run it in
    the threadpool and silently stop tearing it down on client disconnect.
    """
    assert inspect.isasyncgenfunction(get_db), (
        "get_db must stay an async generator: sync generator dependencies are "
        "not torn down when a request is cancelled (client disconnect)."
    )


@pytest.mark.anyio
async def test_session_is_released_when_request_is_cancelled():
    """A cancelled in-flight request must still release its session.

    This exercises the real ``get_db`` dependency against the real engine and
    asserts the pooled connection is handed back *immediately* on cancellation,
    with the garbage collector switched off.
    """
    application = FastAPI()

    @application.get("/slow")
    async def slow(db: Session = Depends(get_db)):  # pragma: no cover - cancelled
        db.execute(text("SELECT 1"))
        await anyio.sleep(30)
        return {"ok": True}

    transport = httpx.ASGITransport(app=application)
    checked_out_before = engine.pool.checkedout()
    gc_was_enabled = gc.isenabled()
    gc.disable()  # prove the teardown does not depend on the collector
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with anyio.create_task_group() as task_group:

                async def issue_request():
                    with anyio.CancelScope(shield=False):
                        await client.get("/slow")

                task_group.start_soon(issue_request)
                await anyio.sleep(0.2)
                task_group.cancel_scope.cancel()
    finally:
        if gc_was_enabled:
            gc.enable()

    assert engine.pool.checkedout() == checked_out_before, (
        "a cancelled request leaked its pooled database connection "
        f"({engine.pool.checkedout()} checked out, expected {checked_out_before})"
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"
