"""File tools: what the model can read and write, and that denied trees are never walked."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from paid_media_agent.harness.files import PathPolicy, PermissionDenied, build_file_tools
from paid_media_agent.harness.tools import ToolContext


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "workspace" / "skills" / "wiki").mkdir(parents=True)
    (tmp_path / "workspace" / "skills" / "wiki" / "SKILL.md").write_text(
        "---\nname: wiki\n---\nTarget CPA: 90\n"
    )
    (tmp_path / "skills").symlink_to("workspace/skills", target_is_directory=True)
    (tmp_path / "workspace" / "state").mkdir()
    (tmp_path / "workspace" / "state" / "pma.duckdb").write_bytes(b"claims")
    (tmp_path / "workspace" / "sources").mkdir()
    (tmp_path / "workspace" / "sources" / "brief.md").write_text("private brief")
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "notes.md").write_text("Target CPA: never read")
    (tmp_path / "outside").mkdir()
    (tmp_path / "workspace" / "escape").symlink_to(tmp_path / ".env")
    return tmp_path


def _run(root: Path, name: str, **args: Any) -> str:
    tools = {t.name: t for t in build_file_tools(root)}
    return str(tools[name].handler(args, ToolContext("t", "u")))


@pytest.mark.parametrize(
    ("operation", "path"),
    [
        ("read", "/.env"),
        ("read", "/skills/../.env"),
        ("read", "/workspace/escape"),
        ("read", "/workspace/state/pma.duckdb"),
        ("read", "/workspace/sources/brief.md"),
        ("write", "/skills/wiki/SKILL.md"),
        ("write", "/workspace/skills/new.md"),
        ("write", "/README.md"),
        ("write", "/workspace/state/forged.duckdb"),
        # Case-insensitive file systems (macOS, Windows) must not open a way around the rules.
        ("read", "/.ENV"),
        ("read", "/workspace/State/pma.duckdb"),
        ("write", "/workspace/SKILLS/x.md"),
        ("write", "/workspace/State/pma.duckdb"),
        # Artifacts and reports are written by code; the model may not forge them.
        ("write", "/workspace/analysis/art_0123456789abcdef.json"),
        ("write", "/workspace/out/rpt_0123.html"),
    ],
)
def test_denied_paths_are_refused_as_written_and_as_resolved(
    root: Path, operation: Any, path: str
) -> None:
    with pytest.raises(PermissionDenied):
        PathPolicy(root).resolve(path, operation)


def test_parent_segments_are_clamped_to_the_project_root(root: Path) -> None:
    resolved = PathPolicy(root).resolve("/../../etc/passwd", "read")
    assert resolved == root.resolve() / "etc" / "passwd"


def test_skills_are_readable_through_the_link_and_workspace_is_writable(root: Path) -> None:
    assert "Target CPA: 90" in _run(root, "read_file", file_path="/skills/wiki/SKILL.md")
    assert _run(root, "write_file", file_path="/workspace/notes/today.md", content="a\nb\n") == (
        "Wrote /workspace/notes/today.md"
    )
    assert "Edited" in _run(
        root, "edit_file", file_path="/workspace/notes/today.md", old_string="b", new_string="c"
    )
    assert (root / "workspace" / "notes" / "today.md").read_text() == "a\nc\n"
    assert "permission denied" in _run(root, "read_file", file_path="/.env")
    assert "permission denied" in _run(root, "write_file", file_path="/README.md", content="x")


def test_search_never_enters_denied_or_hidden_trees(root: Path) -> None:
    found = json.loads(_run(root, "glob", pattern="*.md"))
    assert "/workspace/skills/wiki/SKILL.md" in found
    assert not any(
        p.startswith(("/.venv", "/workspace/sources", "/workspace/state")) for p in found
    )
    hits = _run(root, "grep", pattern="Target CPA")
    assert "/workspace/skills/wiki/SKILL.md:4" in hits and ".venv" not in hits
    listing = json.loads(_run(root, "ls", path="/skills"))
    assert listing == ["/skills/wiki/"]
    assert "/.env" not in json.loads(_run(root, "ls", path="/"))


def test_reads_page_and_edits_refuse_ambiguous_matches(root: Path) -> None:
    (root / "workspace" / "long.md").write_text("\n".join(f"line {i}" for i in range(10)))
    page = _run(root, "read_file", file_path="/workspace/long.md", offset=8, limit=5)
    assert page.splitlines() == ["     9\tline 8", "    10\tline 9"]
    (root / "workspace" / "dup.md").write_text("x x")
    assert "appears 2 times" in _run(
        root, "edit_file", file_path="/workspace/dup.md", old_string="x", new_string="y"
    )
