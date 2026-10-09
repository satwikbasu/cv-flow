from cvflow.discovery.distill import ROLE_FAMILIES
from cvflow.onboarding.bundle import FORM_FIELD_KEYS, PROFILE_DOC_KEYS
from cvflow.onboarding.prompt import build_fix_prompt, build_prompt


def test_build_prompt_contains_contract() -> None:
    out = build_prompt("RESUME BODY XYZ", {"phone": "123"})
    for k in FORM_FIELD_KEYS | PROFILE_DOC_KEYS | set(ROLE_FAMILIES):
        assert k in out
    for macro in ("resumeItem", "resumeSubheading", "resumeProjectHeading",
                  "resumeSubHeadingListStart", "resumeItemListEnd", "section{"):
        assert macro in out
    assert "NEVER" in out and "never" in out.lower()
    assert "RESUME BODY XYZ" in out and "phone: 123" in out
    for key in ("profile_docs", "project_docs", "candidate_skills", "review_notes"):
        assert key in out


def test_build_prompt_custom_taxonomy() -> None:
    assert "zzrole" in build_prompt("r", {}, taxonomy=("zzrole",))


def test_build_fix_prompt() -> None:
    out = build_fix_prompt({"sections/skills.tex": "\\bad"}, "Undefined control sequence")
    assert "sections/skills.tex" in out and "Undefined control sequence" in out
