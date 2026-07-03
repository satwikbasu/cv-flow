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

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from cvflow.analysis import JDAnalysis

__all__ = [
    "Section",
    "Project",
    "Master",
    "parse_master",
    "TailoringPlan",
    "ResumeTailor",
    "TailoringError",
    "MAX_PROJECTS",
]

MAX_PROJECTS = 2  # default project cap (overridden by config resume.max_projects)

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


_NUM_RE = re.compile(r"\d[\d,]*")


def _numbers(text: str) -> set[str]:
    """Digit-runs (commas removed), e.g. '500K'->'500', '10,000'->'10000'. The fact guard blocks
    a reword that introduces a number absent from the original — fabricated metrics (counts, %,
    scale) are the concrete fabrication risk; ordinary synonym rewording is allowed, with the
    user's review of the before/after diff as the backstop for any invented skill or tool."""
    return {m.replace(",", "") for m in _NUM_RE.findall(text)}


class TailoringError(Exception):
    """Raised when a tailoring plan is invalid or would add facts."""


class _Provider(Protocol):
    def generate(self, prompt: str) -> str: ...


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t.rsplit("```", 1)[0]
    return t.strip()


@dataclass(frozen=True)
class TailoringPlan:
    section_order: list[str]
    selected_project_ids: list[str]
    diff_narration: str
    rephrased: dict[str, str] = field(default_factory=dict)


_PLAN_PROMPT = (
    "You tailor a resume by REORDERING sections and SELECTING which projects to show. "
    "You may NOT add, remove, or reword any factual content — only choose order and selection.\n"
    "Return ONLY a JSON object: section_order (ALL the given section names in your chosen "
    "order — reorder for emphasis, do NOT omit any), selected_project_ids (the most relevant "
    "project ids, most relevant first), diff_narration (one short paragraph explaining the "
    "choices in plain language).\n\n"
    "## Available sections (default order)\n{sections}\n\n"
    "## Available projects (id: content)\n{projects}\n\n"
    "## Target job\nrequired_skills: {req}\npreferred_quals: {pref}\nseniority: {sen}\n"
    "\nUser feedback to incorporate (optional): {feedback}\n"
)


_PLAN_WITH_REWRITES_PROMPT = (
    "You tailor a resume. Your PRIMARY job is to REWORD the listed bullets so their wording "
    "mirrors the target job's language and emphasis — this is where most of the tailoring value "
    "is. Reordering sections and selecting projects are SECONDARY (light touches), not the focus.\n"
    "REWORDING HARD RULE: do NOT add, remove, or invent any fact — no new tools, skills, numbers, "
    "employers, metrics, or claims. Only rephrase what each bullet already states; keep every "
    "concrete detail. Make the wording count.\n"
    "Return ONLY a JSON object: rewrites (a list of the reworded bullets in the SAME ORDER as the "
    "numbered bullets below, one string per bullet — this is the main output), section_order (ALL "
    "the given section names in your chosen order — reorder lightly for emphasis, do NOT omit "
    "any), selected_project_ids (the most relevant project ids, most relevant first), "
    "diff_narration (one short paragraph explaining the choices in plain language).\n\n"
    "## Bullets to reword (in order) — THE MAIN TASK\n{bullets}\n\n"
    "## Available sections (default order)\n{sections}\n\n"
    "## Available projects (id: content)\n{projects}\n\n"
    "## Target job\nrequired_skills: {req}\npreferred_quals: {pref}\nseniority: {sen}\n"
    "\nUser feedback to incorporate (optional): {feedback}\n"
)


class ResumeTailor:
    """Plans, renders, fact-checks and diffs a JD-tailored resume."""

    def __init__(
        self,
        provider: _Provider,
        master: Master,
        *,
        max_projects: int = MAX_PROJECTS,
        fact_corpus: str = "",
        rephrase: bool = False,
        disabled_sections: frozenset[str] = frozenset(),
    ) -> None:
        self._provider = provider
        self._master = master
        self._max_projects = max_projects
        self._rephrase = rephrase
        self._disabled_sections = disabled_sections
        # The number guard's allowed set: every digit-run already present anywhere in the master
        # resume + profile. ``fact_corpus`` (the knowledge base + skills) is folded in so a
        # genuine figure the candidate has is never flagged. (No vocabulary check — ordinary
        # synonym rewording is allowed; the user's review of the diff is the backstop.)
        corpus = "\n".join(
            [s.content for s in master.sections.values()]
            + [p.content for p in master.projects]
            + [fact_corpus]
        )
        self._master_numbers = _numbers(corpus)

    def plan(self, jd: JDAnalysis, *, feedback: str | None = None) -> TailoringPlan:
        # One LLM call does ordering + project selection + (when rephrase is on) bullet
        # rewording — a single structured response, so we never spend a second request
        # against the free-tier rate limit.
        candidates = self._candidate_bullets() if self._rephrase else []
        common = {
            "sections": ", ".join(self._master.section_order),
            "projects": "\n".join(
                f"- {p.project_id}: {p.content[:200]}" for p in self._master.projects
            ),
            "req": ", ".join(jd.required_skills),
            "pref": ", ".join(jd.preferred_quals),
            "sen": jd.seniority,
            "feedback": feedback or "(none)",
        }
        if candidates:
            prompt = _PLAN_WITH_REWRITES_PROMPT.format(
                bullets="\n".join(f"{i}. {b}" for i, b in enumerate(candidates)), **common
            )
        else:
            prompt = _PLAN_PROMPT.format(**common)
        raw = self._provider.generate(prompt)
        try:
            d = json.loads(_strip_code_fence(raw))
        except (ValueError, json.JSONDecodeError) as exc:
            raise TailoringError(f"unparseable tailoring plan: {exc}") from exc

        # Never DROP a section — only reorder. Keep the model's order for the sections it
        # listed, then append any it omitted in master order (so nothing is ever lost).
        master_order = list(self._master.section_order)
        valid_sections = set(master_order)
        order: list[str] = []
        seen_sections: set[str] = set()
        for s in d.get("section_order", []):
            if s in valid_sections and s not in seen_sections:
                order.append(s)
                seen_sections.add(s)
        order += [s for s in master_order if s not in seen_sections]
        # User config can drop whole sections entirely (distinct from the LLM, which may never
        # drop an enabled one).
        order = [s for s in order if s not in self._disabled_sections]

        # Projects: the model's most-relevant picks, capped at ``max_projects`` (it returns
        # them most-relevant-first, so truncation keeps the best). Showing FEWER is fine — that's
        # the model judging fewer relevant. Only if it picked NONE do we fall back to master
        # order (up to the cap), so an enabled Projects section is never left empty.
        if "projects" in self._disabled_sections:
            picks: list[str] = []
        else:
            valid_pids = [p.project_id for p in self._master.projects]
            pid_set = set(valid_pids)
            picks = []
            seen_pids: set[str] = set()
            for p in d.get("selected_project_ids", []):
                if p in pid_set and p not in seen_pids:
                    picks.append(p)
                    seen_pids.add(p)
            if not picks:
                picks = list(valid_pids)
            picks = picks[: max(self._max_projects, 0)]

        rephrased = self._accept_rewrites(candidates, d.get("rewrites")) if candidates else {}

        return TailoringPlan(
            section_order=order,
            selected_project_ids=picks,
            diff_narration=str(d.get("diff_narration", "")),
            rephrased=rephrased,
        )

    def _candidate_bullets(self) -> list[str]:
        """Bullets eligible for rewording: experience + every project's (each unless its section
        is disabled), in a stable order fixed BEFORE the call. The model rewords all of them; only
        those in rendered (selected) sections actually reach the PDF via ``_substitute_bullets``."""
        bodies: list[str] = []
        if "experience" not in self._disabled_sections:
            text = self._master.sections.get("experience")
            if text is not None:
                bodies += [b for _, _, b in _resume_item_bodies(text.content)]
        if "projects" not in self._disabled_sections:
            for proj in self._master.projects:
                bodies += [b for _, _, b in _resume_item_bodies(proj.content)]
        return bodies

    def _accept_rewrites(self, candidates: list[str], rewrites: object) -> dict[str, str]:
        """Map each candidate bullet to its reword, keeping ONLY rewords that pass the fact guard
        (best-effort: a missing/malformed ``rewrites`` field degrades to {} — reorder-only — and
        never raises, so a tailoring run is never lost to a bad rewrite payload)."""
        if not isinstance(rewrites, list):
            return {}
        out: dict[str, str] = {}
        for original, reword in zip(candidates, rewrites, strict=False):
            if (
                isinstance(reword, str)
                and reword.strip()
                and reword != original
                and self._guard_ok(original, reword)
            ):
                out[original] = reword
        return out

    def _render_projects(self, selected: list[str]) -> str:
        by_id = {p.project_id: p for p in self._master.projects}
        return "\n".join(by_id[pid].content for pid in selected if pid in by_id)

    def render(self, plan: TailoringPlan) -> str:
        parts: list[str] = []
        for name in plan.section_order:
            if name == "projects":
                raw = self._render_projects(plan.selected_project_ids)
            else:
                raw = self._master.sections[name].content
            parts.append(_substitute_bullets(raw, plan.rephrased))
        return "\n".join(parts)

    def _guard_ok(self, original: str, reword: str) -> bool:
        """True unless ``reword`` introduces a NUMBER absent from ``original``. Ordinary synonym
        rewording (verbs/adjectives/phrasing) is allowed — the only hard, deterministic block is
        a fabricated metric. Any invented skill or tool is caught by the user reviewing the
        before/after diff posted with the PDF."""
        return _numbers(reword) <= _numbers(original)

    def assert_no_new_facts(self, tailored_tex: str) -> None:
        """Raise if the rendered tex introduces a number not present anywhere in the master +
        profile — the deterministic backstop against fabricated metrics. Wording is not checked
        (the per-bullet guard + the user's review of the diff cover invented skills/tools)."""
        extra_numbers = _numbers(tailored_tex) - self._master_numbers
        if extra_numbers:
            raise TailoringError(
                f"tailored resume adds numbers not in master: {sorted(extra_numbers)[:3]}"
            )

    def diff(self, plan: TailoringPlan) -> str:
        lines: list[str] = []
        if plan.rephrased:
            lines.append("Reworded bullets:")
            for original, reword in plan.rephrased.items():
                lines.append(f"  - {original}")
                lines.append(f"  + {reword}")
            lines.append("")
        lines.append("Section order: " + " → ".join(plan.section_order))
        lines.append(
            "Projects shown: " + (", ".join(plan.selected_project_ids) or "(none)")
        )
        lines.append("")
        # Truthful, code-derived summary of what ACTUALLY changed — not the model's prose
        # (which describes its intent before the guard, and tends to overstate).
        n = len(plan.rephrased)
        lines.append(
            f"{n} bullet(s) reworded (the ± lines above); everything else is verbatim from "
            "your master resume."
            if n
            else "No bullets reworded — every line is verbatim from your master resume."
        )
        return "\n".join(lines)
