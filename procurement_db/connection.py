"""Connection lifecycle for procurement PostgreSQL operations."""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from .config import require_database_url


@contextmanager
def get_connection(*, autocommit: bool = False) -> Iterator[Connection]:
    """Yield a dict-row connection and always close it after use.

    The psycopg connection context commits a successful block and rolls back a
    block that raises. Callers should still keep a unit of work small and avoid
    holding this connection while waiting for AI or external API responses.
    """

    with psycopg.connect(
        require_database_url(),
        row_factory=dict_row,
        connect_timeout=10,
        autocommit=autocommit,
        application_name="biddingflow-local",
    ) as connection:
        yield connection
