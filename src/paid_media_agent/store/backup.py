"""State backups: a consistent Parquet export of the state file, and a restore into a new one.

`backup_state` works while `serve` runs (it exports under the store's write lock). Restoring
never overwrites: it builds a new state file from this version's migrations and loads each
exported table's rows, so point `PAID_MEDIA_STATE_PATH` at the result once it looks right.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from paid_media_agent.store.db import Store, utc_now

DEFAULT_KEEP = 14
"""Backups `backup_state` keeps in its folder; older ones are deleted."""


def backups_root(store: Store) -> Path:
    """`backups/` next to the state file."""
    if store.in_memory:
        raise ValueError("the state is in memory; there is no state file to back up")
    return Path(store.path).parent / "backups"


def backup_state(
    store: Store, root: Path, *, keep: int = DEFAULT_KEEP, now: datetime | None = None
) -> tuple[Path, list[Path]]:
    """Export to `root/pma-<UTC stamp>` and delete all but the newest `keep` backups there."""
    stamp = (now or utc_now()).strftime("%Y%m%dT%H%M%SZ")
    target = store.backup(root / f"pma-{stamp}")
    backups = sorted(p for p in root.glob("pma-*") if (p / "load.sql").exists())
    removed = backups[: max(0, len(backups) - keep)]
    for old in removed:
        shutil.rmtree(old)
    return target, removed


def restore_backup(directory: Path, target: Path) -> Path:
    """Load a `Store.backup` export into a new state file; never overwrites an existing one.

    The schema comes from this version's migrations (DuckDB's own import replays views out of
    order), then each exported table's rows are loaded from its Parquet file.
    """
    if target.exists():
        raise FileExistsError(f"{target} already exists; restore to a new path")
    load = directory / "load.sql"
    if not load.exists():
        raise FileNotFoundError(f"{directory} is not a backup (no load.sql)")
    copies = re.findall(r"COPY\s+(\"?[\w.]+\"?)\s+FROM\s+'([^']+)'", load.read_text())
    store = Store(target)
    tables = {
        str(name)
        for (name,) in store.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE'"
        )
    }
    try:
        with store.transaction() as cursor:
            for table, source in copies:
                name = table.strip('"')
                parquet = directory / Path(source).name  # the backup may have been moved
                # Names come from the backup's own load.sql, checked against the new schema.
                if name not in tables:
                    raise ValueError(f"backup table {name} is not in this version's schema")
                cursor.execute(f'DELETE FROM "{name}"')  # noqa: S608 - rows the migrations seeded
                cursor.execute(
                    f'INSERT INTO "{name}" SELECT * FROM read_parquet(?)',  # noqa: S608
                    [str(parquet)],
                )
    except BaseException:
        store.close()
        target.unlink(missing_ok=True)
        raise
    store.close()
    return target
