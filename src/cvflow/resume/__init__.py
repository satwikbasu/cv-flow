"""Resume tailoring: pick and reorder pieces of a modular LaTeX master resume.

Tailoring works at the granularity of whole existing units — sections and
per-project blocks of the modular master. By default it only selects and
reorders them; it never rewrites unit text, which makes "no new facts" a
deterministic, exact check. The optional rephrase mode (off unless enabled in
config) lets the model reword bullets, guarded so a reword can never introduce
a number that was not already there, with the before/after diff shown to the
user either way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Section",
    "Project",
    "Master",
    "parse_master",
]

_INPUT_RE = re.compile(r"\\input\{sections/([^}]+?)\.tex\}")
_PROJECT_INPUT_RE = re.compile(r"\\input\{sections/projects/([^}]+?)\.tex\}")

_RESUME_ITEM_OPEN = "\\resumeItem{"


def _resume_item_bodies(text: str) -> list[tuple[int, int, str]]:
    """Find each ``\\resumeItem{...}`` and return (body_start, body_end, body), balancing
    braces so nested ``{...}`` (e.g. ``\\textbf{}``) is handled. ``body_end`` is the index
    of the matching close brace (exclusive of it)."""
    spans: list[tuple[int, int, str]] = []
    i = 0
    while True:
        j = text.find(_RESUME_ITEM_OPEN, i)
        if j == -1:
            break
        start = j + len(_RESUME_ITEM_OPEN)
        depth = 1
        k = start
        while k < len(text) and depth:
            if text[k] == "{":
                depth += 1
            elif text[k] == "}":
                depth -= 1
            k += 1
        end = k - 1  # index of the matching close brace
        spans.append((start, end, text[start:end]))
        i = k
    return spans


def _substitute_bullets(text: str, rephrased: dict[str, str]) -> str:
    """Replace each ``\\resumeItem`` body with ``rephrased[body]`` when present (else leave it)."""
    out: list[str] = []
    last = 0
    for start, end, body in _resume_item_bodies(text):
        out.append(text[last:start])
        out.append(rephrased.get(body, body))
        last = end
    out.append(text[last:])
    return "".join(out)


@dataclass(frozen=True)
class Section:
    name: str
    content: str


@dataclass(frozen=True)
class Project:
    project_id: str
    content: str


@dataclass(frozen=True)
class Master:
    root: Path
    section_order: list[str]
    sections: dict[str, Section]
    projects: list[Project]


def parse_master(root: str | Path) -> Master:
    """Parse the modular ``master.tex`` into ordered sections + selectable projects."""
    root = Path(root)
    master_tex = (root / "master.tex").read_text()
    order: list[str] = []
    sections: dict[str, Section] = {}
    for name in _INPUT_RE.findall(master_tex):
        if name.startswith("projects/"):
            continue  # nested per-project inputs are handled below
        order.append(name)
        sections[name] = Section(
            name=name, content=(root / "sections" / f"{name}.tex").read_text()
        )
    projects: list[Project] = []
    projects_tex = root / "sections" / "projects.tex"
    if projects_tex.exists():
        for pid in _PROJECT_INPUT_RE.findall(projects_tex.read_text()):
            projects.append(
                Project(
                    project_id=pid,
                    content=(root / "sections" / "projects" / f"{pid}.tex").read_text(),
                )
            )
    return Master(root=root, section_order=order, sections=sections, projects=projects)
