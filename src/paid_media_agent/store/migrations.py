"""Numbered schema migrations. Append new ones; never edit a migration that has shipped."""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb

OPERATIONAL = """
CREATE TABLE proposals (
    proposal_id UUID PRIMARY KEY,
    routing_id VARCHAR NOT NULL UNIQUE,
    thread_id VARCHAR NOT NULL,
    state VARCHAR NOT NULL,
    record JSON NOT NULL,
    record_sha VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
CREATE TABLE approvals (
    claim_id UUID PRIMARY KEY,
    proposal_id UUID NOT NULL,
    revision INTEGER NOT NULL,
    approved_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP,
    claim JSON NOT NULL
);
CREATE TABLE receipts (
    proposal_id UUID PRIMARY KEY,
    receipt JSON NOT NULL,
    created_at TIMESTAMP NOT NULL
);
CREATE TABLE dedupe (
    dedupe_key VARCHAR PRIMARY KEY,
    seen_at TIMESTAMP NOT NULL
);
CREATE TABLE threads (
    thread_id VARCHAR PRIMARY KEY,
    owner_ref VARCHAR NOT NULL,
    created_at TIMESTAMP NOT NULL
);
"""

MIGRATIONS: tuple[tuple[str, str], ...] = (("0001_operational", OPERATIONAL),)


def apply_migrations(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version VARCHAR PRIMARY KEY, applied_at TIMESTAMP NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    for version, sql in MIGRATIONS:
        if version in applied:
            continue
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, ?)",
                [version, datetime.now(UTC).replace(tzinfo=None)],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
