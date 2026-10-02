"""DuckDB behaviour the state store depends on. A DuckDB upgrade that changes any of it fails here."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import duckdb
import pytest


def _proposals(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        "CREATE TABLE proposals(proposal_id VARCHAR PRIMARY KEY, routing_id VARCHAR UNIQUE, "
        "state VARCHAR, record_sha VARCHAR)"
    )
    con.execute("INSERT INTO proposals VALUES ('p1', 'r1', 'draft', 'sha-1')")


def test_insert_on_conflict_returning_reports_first_writer_only() -> None:
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE dedupe(dedupe_key VARCHAR PRIMARY KEY)")
    sql = "INSERT INTO dedupe VALUES ('k') ON CONFLICT DO NOTHING RETURNING dedupe_key"
    assert con.execute(sql).fetchall() == [("k",)]
    assert con.execute(sql).fetchall() == []


def test_update_returning_is_a_compare_and_swap_on_rows_with_unique_columns() -> None:
    con = duckdb.connect(":memory:")
    _proposals(con)
    cas = (
        "UPDATE proposals SET state = ?, record_sha = ? "
        "WHERE proposal_id = 'p1' AND record_sha = ? RETURNING proposal_id"
    )
    assert len(con.execute(cas, ["approved", "sha-2", "sha-1"]).fetchall()) == 1
    assert con.execute(cas, ["executed", "sha-3", "sha-1"]).fetchall() == []
    for step in range(20):
        con.execute(cas, [f"s{step}", f"sha-{step + 3}", f"sha-{step + 2}"])
    assert con.execute("SELECT routing_id FROM proposals").fetchall() == [("r1",)]
    # A revision rotates routing_id; the unique index must accept it and still reject duplicates.
    con.execute("INSERT INTO proposals VALUES ('p2', 'taken', 'draft', 'sha-x')")
    con.execute("UPDATE proposals SET routing_id = 'r2' WHERE proposal_id = 'p1'")
    with pytest.raises(duckdb.ConstraintException):
        con.execute("UPDATE proposals SET routing_id = 'taken' WHERE proposal_id = 'p1'")


def test_concurrent_updates_to_one_row_raise_transaction_exception() -> None:
    con = duckdb.connect(":memory:")
    _proposals(con)
    first, second = con.cursor(), con.cursor()
    first.execute("BEGIN")
    second.execute("BEGIN")
    first.execute("UPDATE proposals SET state = 'a' WHERE proposal_id = 'p1'")
    with pytest.raises(duckdb.TransactionException):
        second.execute("UPDATE proposals SET state = 'b' WHERE proposal_id = 'p1'")
    first.execute("COMMIT")


def test_racing_compare_and_swaps_have_exactly_one_winner() -> None:
    con = duckdb.connect(":memory:")
    _proposals(con)
    barrier = threading.Barrier(2)
    outcomes: list[bool] = []

    def race(tag: str) -> None:
        cursor = con.cursor()
        barrier.wait()
        try:
            rows = cursor.execute(
                "UPDATE proposals SET state = ?, record_sha = ? "
                "WHERE proposal_id = 'p1' AND record_sha = 'sha-1' RETURNING 1",
                [tag, f"sha-{tag}"],
            ).fetchall()
            outcomes.append(len(rows) == 1)
        except duckdb.TransactionException:
            outcomes.append(False)

    threads = [threading.Thread(target=race, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == [False, True]


def test_naive_timestamps_fetch_without_optional_timezone_modules() -> None:
    value = duckdb.connect(":memory:").execute("SELECT TIMESTAMP '2026-09-25 12:00:00'").fetchone()
    assert value is not None and value[0].tzinfo is None


def test_a_second_process_cannot_open_a_file_held_for_writing(tmp_path: Path) -> None:
    path = tmp_path / "state.duckdb"
    held = duckdb.connect(str(path))
    probe = (
        "import duckdb, sys\n"
        "for read_only in (True, False):\n"
        "    try:\n"
        f"        duckdb.connect({str(path)!r}, read_only=read_only)\n"
        "        print('opened')\n"
        "    except duckdb.IOException:\n"
        "        print('locked')\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)  # noqa: S603
    held.close()
    assert result.stdout.split() == ["locked", "locked"]
