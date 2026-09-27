"""Progressive disclosure of runtime skills: a short index in the prompt, bodies read on demand."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: str


def frontmatter(text: str) -> dict[str, str]:
    """`key: value` lines between the opening `---` fences. Skills use only one-line values."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        key, sep, value = line.partition(":")
        if sep and key.strip():
            fields[key.strip()] = value.strip().strip("\"'")
    return {}


def discover_skills(skills_dir: Path, *, virtual_root: str = "/skills") -> tuple[Skill, ...]:
    """Every `<name>/SKILL.md` with `name` and `description` frontmatter, in name order."""
    skills: list[Skill] = []
    if not skills_dir.is_dir():
        return ()
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        front = frontmatter(path.read_text(encoding="utf-8"))
        if front.get("name") and front.get("description"):
            skills.append(
                Skill(
                    front["name"],
                    front["description"],
                    f"{virtual_root}/{path.parent.name}/SKILL.md",
                )
            )
    return tuple(skills)


def skills_prompt(skills: tuple[Skill, ...]) -> str:
    if not skills:
        return ""
    lines = [
        "## Skills",
        "Before a task a skill covers, read its SKILL.md with read_file and follow it. Read the "
        "pages it links to only when you need them.",
        "",
        *(f"- **{s.name}** ({s.path}): {s.description}" for s in skills),
    ]
    return "\n".join(lines)
