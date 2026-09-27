"""File tools over the repository: skills and wiki reads, workspace notes, nothing else.

Paths are virtual and rooted at the repository (`/workspace/note.md`). Every path is checked
twice, as written and after resolving symlinks, so `/skills/..` or a link cannot reach a denied
file. Denied paths answer "permission denied" and nothing else.
"""

from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from paid_media_agent.harness.tools import ToolContext, ToolSpec, parameters_for

Operation = Literal["read", "write"]
DENY_ALL = (
    "/.env",
    "/.env.*",
    "/.venv/**",
    "/.git/**",
    "/workspace/state/**",
    "/.agents/**",
    "/.claude/**",
    "/config/**",
    "/workspace/sources/**",
    "/docs/org/**",
)
DENY_WRITE = ("/workspace/skills/**",)
ALLOW_WRITE = ("/workspace/**",)
READ_LIMIT = 2000
LINE_CHARS = 2000
MAX_MATCHES = 200


def _matches(path: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("/**"):
            base = pattern[:-3]
            if path == base or path.startswith(base + "/"):
                return True
        elif fnmatch.fnmatchcase(path, pattern):
            return True
    return False


class PermissionDenied(Exception):
    pass


@dataclass(frozen=True)
class PathPolicy:
    root: Path

    def resolve(self, virtual: str, operation: Operation) -> Path:
        cleaned = posixpath.normpath("/" + virtual.strip().lstrip("/"))
        real_root = self.root.resolve()
        target = (real_root / cleaned.lstrip("/")).resolve()
        if target != real_root and real_root not in target.parents:
            raise PermissionDenied(virtual)
        resolved = "/" + target.relative_to(real_root).as_posix() if target != real_root else "/"
        for path in {cleaned, resolved}:
            if _matches(path, DENY_ALL):
                raise PermissionDenied(virtual)
            if operation == "write" and (
                _matches(path, DENY_WRITE) or not _matches(path, ALLOW_WRITE)
            ):
                raise PermissionDenied(virtual)
        return target

    def readable(self, virtual: str) -> bool:
        try:
            self.resolve(virtual, "read")
        except (PermissionDenied, ValueError, OSError):
            return False
        return True

    def walk(self, virtual: str) -> Iterator[str]:
        """Readable files under a virtual directory, never descending into denied or hidden ones."""
        base = self.resolve(virtual, "read")
        start = posixpath.normpath("/" + virtual.strip().lstrip("/"))
        if base.is_file():
            yield start
            return
        for directory, dirnames, filenames in os.walk(base):
            relative = Path(directory).relative_to(base).as_posix()
            here = start if relative == "." else posixpath.join(start, relative)
            dirnames[:] = sorted(
                d
                for d in dirnames
                if not d.startswith(".") and self.readable(posixpath.join(here, d))
            )
            for name in sorted(filenames):
                candidate = posixpath.join(here, name)
                if self.readable(candidate):
                    yield candidate


class _Ls(BaseModel):
    path: str = Field(default="/", description="Directory to list, e.g. /skills.")


class _ReadFile(BaseModel):
    file_path: str = Field(
        description="Absolute virtual path, e.g. /skills/paid-media-wiki/SKILL.md."
    )
    offset: int = Field(default=0, ge=0, description="Line to start from (0-based).")
    limit: int = Field(default=READ_LIMIT, ge=1, le=READ_LIMIT, description="Lines to read.")


class _WriteFile(BaseModel):
    file_path: str = Field(description="Path under /workspace, e.g. /workspace/notes.md.")
    content: str


class _EditFile(BaseModel):
    file_path: str
    old_string: str = Field(description="Exact text to replace.")
    new_string: str
    replace_all: bool = False


class _Glob(BaseModel):
    pattern: str = Field(description="Glob such as **/*.md.")
    path: str = "/"


class _Grep(BaseModel):
    pattern: str = Field(description="Regular expression.")
    path: str = "/"
    glob: str | None = Field(default=None, description="Only files matching this glob.")


def build_file_tools(root: Path) -> list[ToolSpec]:
    policy = PathPolicy(root)

    def guarded(operation: Operation, handler: Any) -> Any:
        def run(args: dict[str, Any], _context: ToolContext) -> str:
            try:
                return str(handler(args))
            except PermissionDenied as exc:
                return f"Error: permission denied for {operation} on {exc}"
            except FileNotFoundError:
                return "Error: file not found"
            except (IsADirectoryError, NotADirectoryError, UnicodeDecodeError) as exc:
                return f"Error: {type(exc).__name__}"

        return run

    def ls(args: dict[str, Any]) -> str:
        requested = posixpath.normpath("/" + args.get("path", "/").strip().lstrip("/"))
        directory = policy.resolve(requested, "read")
        entries = []
        for child in sorted(directory.iterdir()):
            virtual = posixpath.join(requested, child.name)
            if not child.name.startswith(".") and policy.readable(virtual):
                entries.append(virtual + ("/" if child.is_dir() else ""))
        return json.dumps(entries)

    def read_file(args: dict[str, Any]) -> str:
        path = policy.resolve(args["file_path"], "read")
        lines = path.read_text(encoding="utf-8").splitlines()
        offset, limit = int(args.get("offset", 0)), int(args.get("limit", READ_LIMIT))
        chunk = lines[offset : offset + limit]
        if not chunk:
            return "(empty)" if not lines else f"Error: offset {offset} is past the end"
        return "\n".join(f"{offset + i + 1:6}\t{line[:LINE_CHARS]}" for i, line in enumerate(chunk))

    def write_file(args: dict[str, Any]) -> str:
        path = policy.resolve(args["file_path"], "write")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        return f"Wrote {args['file_path']}"

    def edit_file(args: dict[str, Any]) -> str:
        path = policy.resolve(args["file_path"], "write")
        text = path.read_text(encoding="utf-8")
        count = text.count(args["old_string"])
        if count == 0:
            return "Error: old_string not found"
        if count > 1 and not args.get("replace_all"):
            return f"Error: old_string appears {count} times; pass replace_all or add context"
        updated = text.replace(
            args["old_string"], args["new_string"], -1 if args.get("replace_all") else 1
        )
        path.write_text(updated, encoding="utf-8")
        return f"Edited {args['file_path']} ({count if args.get('replace_all') else 1} replacement)"

    def _selected(base: str, pattern: str) -> Iterator[str]:
        root = posixpath.normpath("/" + base.strip().lstrip("/"))
        for virtual in policy.walk(root):
            relative = posixpath.relpath(virtual, root)
            if fnmatch.fnmatchcase(relative, pattern) or fnmatch.fnmatchcase(
                posixpath.basename(virtual), pattern
            ):
                yield virtual

    def glob(args: dict[str, Any]) -> str:
        found: list[str] = []
        for virtual in _selected(args.get("path", "/"), args["pattern"]):
            found.append(virtual)
            if len(found) >= MAX_MATCHES:
                break
        return json.dumps(found)

    def grep(args: dict[str, Any]) -> str:
        expression = re.compile(args["pattern"])
        hits: list[str] = []
        for virtual in _selected(args.get("path", "/"), args.get("glob") or "*"):
            try:
                lines = policy.resolve(virtual, "read").read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError, PermissionDenied):
                continue
            for number, line in enumerate(lines, 1):
                if expression.search(line):
                    hits.append(f"{virtual}:{number}: {line[:300]}")
                    if len(hits) >= MAX_MATCHES:
                        return "\n".join(hits)
        return "\n".join(hits) or "(no matches)"

    def spec(
        name: str, description: str, model: type[BaseModel], operation: Operation, handler: Any
    ) -> ToolSpec:
        return ToolSpec(
            name=name,
            description=description,
            parameters=parameters_for(model),
            handler=guarded(operation, handler),
            kind="file",
            offload=False,
        )

    return [
        spec("ls", "List a directory in the project, e.g. /skills or /workspace.", _Ls, "read", ls),
        spec(
            "read_file",
            "Read a text file with line numbers. Page long files with offset and limit.",
            _ReadFile,
            "read",
            read_file,
        ),
        spec(
            "write_file",
            "Write a file under /workspace. Skills and configuration are read-only.",
            _WriteFile,
            "write",
            write_file,
        ),
        spec(
            "edit_file",
            "Replace exact text in a file under /workspace.",
            _EditFile,
            "write",
            edit_file,
        ),
        spec("glob", "Find files by glob pattern.", _Glob, "read", glob),
        spec("grep", "Search file contents with a regular expression.", _Grep, "read", grep),
    ]
