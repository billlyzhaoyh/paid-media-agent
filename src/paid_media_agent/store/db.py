"""One DuckDB database per process: operational state, conversations, and analytics history.

DuckDB lets a single process hold a database file for writing, and while it does, no other
process can open the file at all, even read-only. `serve` owns the file for its lifetime; other
processes get `StoreBusy`. Tests, the demo, and local runs use an in-memory database.

Worker threads each get their own cursor. Writes take one lock and run as single statements, so
compare-and-swap updates are decided by their `WHERE` clause and never interleave.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb

from paid_media_agent.store.migrations import apply_migrations

if TYPE_CHECKING:
    from paid_media_agent.store.operational import OperationalRepositories

MEMORY = ":memory:"


class StoreBusy(RuntimeError):
    """Another process holds the database file, usually `paid-media-agent serve`."""


class StoreConflict(RuntimeError):
    """A concurrent write touched the same row; the caller treats it as a lost race."""


def utc_now() -> datetime:
    """Naive UTC. DuckDB returns TIMESTAMPTZ only through pytz, which is not a dependency."""
    return datetime.now(UTC).replace(tzinfo=None)


def naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


class Store:
    def __init__(self, path: Path | str = MEMORY) -> None:
        target = str(path)
        if target != MEMORY:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = duckdb.connect(target)
        except duckdb.IOException as exc:
            if "lock" in str(exc).lower():
                raise StoreBusy(
                    f"{target} is held by another process; stop `paid-media-agent serve` or use "
                    "its API"
                ) from None
            raise
        self.path = target
        self._write_lock = threading.Lock()
        self._local = threading.local()
        apply_migrations(self._conn)

    @property
    def in_memory(self) -> bool:
        return self.path == MEMORY

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        cursor: duckdb.DuckDBPyConnection | None = getattr(self._local, "cursor", None)
        if cursor is None:
            cursor = self._conn.cursor()
            self._local.cursor = cursor
        return cursor

    def fetch(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        return self._cursor().execute(sql, list(params)).fetchall()

    def fetch_dicts(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        result = self._cursor().execute(sql, list(params))
        names = [column[0] for column in result.description or ()]
        return [dict(zip(names, row, strict=True)) for row in result.fetchall()]

    def write(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        """Run one write statement under the process write lock and return its rows."""
        with self._write_lock:
            try:
                return self._cursor().execute(sql, list(params)).fetchall()
            except duckdb.TransactionException as exc:
                raise StoreConflict(str(exc)) from None

    @contextmanager
    def transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Several statements under the write lock that commit together or not at all."""
        with self._write_lock:
            cursor = self._cursor()
            cursor.execute("BEGIN TRANSACTION")
            try:
                yield cursor
            except BaseException:
                cursor.execute("ROLLBACK")
                raise
            try:
                cursor.execute("COMMIT")
            except duckdb.TransactionException as exc:
                raise StoreConflict(str(exc)) from None

    @cached_property
    def repositories(self) -> OperationalRepositories:
        from paid_media_agent.store.operational import OperationalRepositories

        return OperationalRepositories(self)

    def backup(self, directory: Path) -> Path:
        """Export every table as Parquet under the write lock, so the copy is consistent.

        `restore_backup` loads it into a new state file. Works while `serve` holds the file.
        """
        if directory.exists():
            raise FileExistsError(f"{directory} already exists")
        directory.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            self._cursor().execute(f"EXPORT DATABASE '{sql_path(directory)}' (FORMAT PARQUET)")
        return directory

    def close(self) -> None:
        self._conn.close()


def sql_path(path: Path) -> str:
    text = str(path)
    if "'" in text:
        raise ValueError("backup paths may not contain quotes")
    return text


def json_rows(shape: Mapping[str, str]) -> str:
    """A `SELECT` over one JSON parameter holding a list of row objects typed by `shape`.

    Binding the rows as a single string is far faster than per-row parameters: DuckDB parses and
    casts the whole batch in one statement. Decimals travel as strings so no precision is lost.
    """
    return f"SELECT unnest(from_json(?::JSON, '{json.dumps([dict(shape)])}'), recursive := true)"
