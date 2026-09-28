"""State backups: a consistent export while the store is open, restored into a new file only."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from paid_media_agent.cli import main
from paid_media_agent.store import Store
from paid_media_agent.store.backup import backup_state, restore_backup


def test_a_backup_restores_into_a_new_file_and_old_backups_rotate(tmp_path: Path) -> None:
    store = Store(tmp_path / "pma.duckdb")
    store.repositories.dedupe.seen("delivery-1")
    root = tmp_path / "backups"
    first, _ = backup_state(store, root, now=datetime(2026, 9, 1, tzinfo=UTC))
    for day in range(2, 5):
        _, removed = backup_state(store, root, keep=2, now=datetime(2026, 9, day, tzinfo=UTC))
    assert sorted(p.name for p in root.iterdir()) == [
        "pma-20260903T000000Z",
        "pma-20260904T000000Z",
    ]
    assert not first.exists()

    moved = tmp_path / "elsewhere" / "latest"
    moved.parent.mkdir()
    (root / "pma-20260904T000000Z").rename(moved)
    restored = restore_backup(moved, tmp_path / "restored.duckdb")
    copy = Store(restored)
    assert copy.fetch("SELECT dedupe_key FROM dedupe") == [("delivery-1",)]
    assert copy.fetch("SELECT count(*) FROM maturity_days") == store.fetch(
        "SELECT count(*) FROM maturity_days"
    ), "seeded rows are replaced, not doubled"
    with pytest.raises(FileExistsError):
        restore_backup(moved, restored)


def test_dedupe_keys_are_pruned_after_their_window(tmp_path: Path) -> None:
    store = Store()
    dedupe = store.repositories.dedupe
    assert dedupe.seen("old") is False and dedupe.seen("old") is True
    assert dedupe.prune(datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=1)) == 1
    assert dedupe.seen("old") is False, "a pruned key counts as new again"


def test_the_cli_backs_up_restores_and_checks_the_live_path_on_sample_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = datetime.now(UTC).date()
    monkeypatch.setenv("PAID_MEDIA_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("PAID_MEDIA_STATE_PATH", str(tmp_path / "state" / "pma.duckdb"))
    monkeypatch.setenv("PAID_MEDIA_DATA_MODE", "sample")
    monkeypatch.setenv("PAID_MEDIA_FIXTURE_ANCHOR", (today - timedelta(days=2)).isoformat())
    runner = CliRunner()
    assert runner.invoke(main, ["sync"]).exit_code == 0

    made = runner.invoke(main, ["backup"])
    assert made.exit_code == 0, made.output
    backups = list((tmp_path / "state" / "backups").iterdir())
    assert len(backups) == 1 and str(backups[0]) in made.output
    target = tmp_path / "restored.duckdb"
    restored = runner.invoke(main, ["restore", str(backups[0]), "--to", str(target)])
    assert restored.exit_code == 0, restored.output
    assert runner.invoke(main, ["restore", str(backups[0]), "--to", str(target)]).exit_code != 0
    assert Store(target).fetch("SELECT count(*) FROM entity_daily_snapshots")[0][0] > 0

    checked = runner.invoke(main, ["doctor", "--live", "--alias", "demo-google", "--json"])
    # WeasyPrint prints install hints to stdout where its native libraries are missing.
    report, _ = json.JSONDecoder().raw_decode(checked.output[checked.output.index("{") :])
    checks = {c["name"]: c for c in report["checks"]}
    assert checks["live:demo-google:contract"]["detail"] == "rows, host"
    assert checks["live:demo-google:read"]["status"] == "ok"
    assert checks["account_config"]["status"] == "ok", "the example file is fine in sample mode"
