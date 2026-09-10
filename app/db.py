"""Postgres access.

The jobs table doubles as the work queue. That is a deliberate simplification:
at this volume a dedicated queue would be another service to run and another
thing to lose state in, while `FOR UPDATE SKIP LOCKED` gives correct
at-most-once claiming with nothing extra to deploy.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import settings

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=10,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


@contextmanager
def connection() -> Iterator[Connection]:
    with pool().connection() as conn:
        yield conn


def query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connection() as conn:
        return conn.execute(sql, params).fetchall()


def query_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple[Any, ...] = ()) -> None:
    with connection() as conn:
        conn.execute(sql, params)


def apply_schema() -> None:
    """Create tables if they do not exist, then seed the admin allowlist.

    Safe to call on every boot of every process.
    """
    schema = (Path(__file__).parent / "schema.sql").read_text()
    with connection() as conn:
        conn.execute(schema)
        for email in settings.admin_emails:
            conn.execute(
                """
                INSERT INTO invites (email, invited_by)
                VALUES (%s, 'bootstrap')
                ON CONFLICT (email) DO NOTHING
                """,
                (email,),
            )
            # An address listed in ADMIN_EMAILS is promoted even if it already
            # exists, so admin rights can be granted by redeploying.
            conn.execute(
                "UPDATE users SET is_admin = TRUE WHERE email = %s", (email,)
            )
    log.info("schema applied; %d bootstrap admin(s)", len(settings.admin_emails))


def wait_for_database(attempts: int = 30, delay: float = 1.0) -> None:
    """Block until Postgres answers.

    Compose starts the app and the database together, so the first few
    connections routinely fail. Retrying here is simpler than a healthcheck
    dependency and gives a clearer log line when it is genuinely unreachable.
    """
    import time

    from psycopg import OperationalError

    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with connection() as conn:
                conn.execute("SELECT 1")
            return
        except OperationalError as exc:  # pragma: no cover - timing dependent
            last = exc
            global _pool
            if _pool is not None:
                _pool.close()
                _pool = None
            log.info("waiting for database (%d/%d)", attempt, attempts)
            time.sleep(delay)
    raise RuntimeError(f"database unreachable after {attempts} attempts: {last}")
