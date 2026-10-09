"""Prompt builders for LLM-driven onboarding. Pure functions: no I/O, no LLM calls."""

from __future__ import annotations

from cvflow.discovery.distill import ROLE_FAMILIES
from cvflow.onboarding.bundle import FORM_FIELD_KEYS, PROFILE_DOC_KEYS

_RULES = r"""RULES
1. NEVER invent facts. Use ONLY what appears in the resume text or USER FACTS below. Anything
   unknown -> "" for a form field, omit it from skills, and ADD a line to review_notes naming
   what is missing. Never guess emails, phones, dates, salaries, or URLs.
2. resume.sections use ONLY these LaTeX macros from the template: \resumeItem{..},
   \resumeSubheading{..}{..}{..}{..}, \resumeProjectHeading{..}{..},
   \resumeSubHeadingListStart / \resumeSubHeadingListEnd, \resumeItemListStart /
   \resumeItemListEnd, and \section{..}. Escape LaTeX specials (& % $ # _).
   Skills section example:
   \section{Technical Skills}
      \begin{itemize}[leftmargin=0.15in, label={}]
      \small{\item{ \textbf{Languages}{: Python, Go} \\ }}
      \end{itemize}
   Project block example (one per project_blocks value):
   \resumeProjectHeading{\textbf{Name} $|$ \emph{Tech, Stack}}{}
      \resumeItemListStart
      \resumeItem{What was built}
      \resumeItemListEnd
3. resume.sections is an ORDERED map of section name -> LaTeX body (e.g. experience,
   education, projects, skills). INCLUDE a "projects" key positioned where projects should
   appear, but leave its body "" (the projects file is assembled from project_blocks).
   Each project_blocks[slug] is ONE \resumeProjectHeading{..}{..} block with \resumeItem
   bullets. project_blocks slugs MUST equal project_docs slugs (lowercase-hyphen).
4. candidate_skills.skills must mirror the skills stated in the resume. synonyms map common
   aliases (e.g. "k8s") to a canonical skill that IS in skills.
5. discovery and preferences are config blocks. preferences.prefer_roles keys MUST come from
   the role taxonomy. Base role weights (0-1) on the candidate's actual background. Do not
   invent a target salary: leave min_ctc_lpa / top_ctc_lpa to USER FACTS, or use neutral
   defaults and say so in review_notes.
"""


def build_prompt(
    resume_text: str,
    user_facts: dict[str, str],
    *,
    taxonomy: tuple[str, ...] = ROLE_FAMILIES,
) -> str:
    docs = ", ".join(f'"{k}"' for k in sorted(PROFILE_DOC_KEYS))
    fields = ", ".join(f'"{k}"' for k in sorted(FORM_FIELD_KEYS))
    facts = "\n".join(f"- {k}: {v}" for k, v in user_facts.items()) or "(none provided)"
    return (
        "You convert a candidate's resume into a cvflow onboarding bundle. Return ONE JSON "
        "object and nothing else (no prose, no code fence) with exactly these top-level keys:\n"
        "- profile_docs: object with exactly the keys " + docs + "; each value is Markdown.\n"
        "- project_docs: object slug -> Markdown describing that project.\n"
        "- form_fields: object with exactly the keys " + fields + "; string values, \"\" if "
        "unknown.\n"
        "- candidate_skills: {\"skills\": [str], \"synonyms\": {alias: canonical skill}}.\n"
        "- discovery: object of job-search config (search_terms, locations, etc.).\n"
        "- preferences: object of ranking config (prefer_roles: {role: weight}, "
        "fit_weight, comp_weight summing to 1.0, min_ctc_lpa, top_ctc_lpa, etc.).\n"
        "- resume: {\"heading_tex\": LaTeX heading block, \"sections\": ordered "
        "{name: LaTeX body}, \"project_blocks\": {slug: LaTeX block}}.\n"
        "- review_notes: [str] of everything missing, assumed, or needing the user's check.\n\n"
        + _RULES
        + "\nRole taxonomy for preferences.prefer_roles keys: "
        + ", ".join(taxonomy)
        + "\n\nUSER FACTS\n"
        + facts
        + "\n\nRESUME TEXT\n"
        + resume_text
        + "\n"
    )


def build_fix_prompt(failing_files: dict[str, str], compile_errors: str) -> str:
    files = "\n\n".join(f"=== {name} ===\n{body}" for name, body in failing_files.items())
    return (
        "These LaTeX files failed to compile. Fix ONLY the LaTeX errors; do not add, remove, "
        "or change any facts. Return a single JSON object mapping each file name below to its "
        "corrected full contents, and include only these files.\n\n"
        f"COMPILE ERRORS\n{compile_errors}\n\nFILES\n{files}\n"
    )


def _facts_block(user_facts: dict[str, str]) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in user_facts.items()) or "(none provided)"


def build_stage1_prompt(
    resume_text: str,
    user_facts: dict[str, str],
    *,
    taxonomy: tuple[str, ...] = ROLE_FAMILIES,
) -> str:
    """Stage 1 of two-stage onboarding: the FACTS subset (docs, form fields, LaTeX)."""
    docs = ", ".join(f'"{k}"' for k in sorted(PROFILE_DOC_KEYS))
    fields = ", ".join(f'"{k}"' for k in sorted(FORM_FIELD_KEYS))
    return (
        "You convert a candidate's resume into the FACTS half of a cvflow onboarding bundle. "
        "Return ONE JSON object and nothing else with exactly these top-level keys:\n"
        "- profile_docs: object with exactly the keys " + docs + "; each value is Markdown.\n"
        "- project_docs: object slug -> Markdown describing that project.\n"
        "- form_fields: object with exactly the keys " + fields + "; string values, \"\" if "
        "unknown.\n"
        "- resume: {\"heading_tex\": LaTeX heading block, \"sections\": ordered "
        "{name: LaTeX body}, \"project_blocks\": {slug: LaTeX block}}.\n"
        "- review_notes: [str] of everything missing, assumed, or needing the user's check.\n\n"
        + _RULES
        + "\nRole taxonomy (for later stages): "
        + ", ".join(taxonomy)
        + "\n\nUSER FACTS\n"
        + _facts_block(user_facts)
        + "\n\nRESUME TEXT\n"
        + resume_text
        + "\n"
    )


def build_stage2_prompt(stage1_payload: str, *, taxonomy: tuple[str, ...] = ROLE_FAMILIES) -> str:
    """Stage 2: DERIVED blocks, computed from stage 1's output so they stay in sync."""
    return (
        "Given the candidate's already-generated profile below (STAGE 1 OUTPUT), derive the "
        "config half of the onboarding bundle. Return ONE JSON object and nothing else with "
        "exactly these top-level keys:\n"
        "- candidate_skills: {\"skills\": [str], \"synonyms\": {alias: canonical skill}}; "
        "skills must mirror the skills doc and resume in STAGE 1 OUTPUT; every synonym target "
        "must be in skills.\n"
        "- discovery: object of job-search config (search_terms, locations, etc.).\n"
        "- preferences: object of ranking config (prefer_roles: {role: weight 0-1}, "
        "fit_weight, comp_weight summing to 1.0, min_ctc_lpa, top_ctc_lpa, etc.).\n"
        "- review_notes: [str] of anything assumed or defaulted here that the user must check.\n\n"
        "NEVER invent facts: use only STAGE 1 OUTPUT. Do not invent a target salary; use "
        "neutral defaults and say so in review_notes. preferences.prefer_roles keys MUST come "
        "from the role taxonomy: " + ", ".join(taxonomy)
        + "\n\nSTAGE 1 OUTPUT\n" + stage1_payload + "\n"
    )
