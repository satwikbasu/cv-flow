from cvflow.onboarding.bundle import ResumeBundle
from cvflow.onboarding.factcheck import unverified_numbers


def _resume(**kw: object) -> ResumeBundle:
    base: dict[str, object] = {"heading_tex": "", "sections": {}, "project_blocks": {}}
    base.update(kw)
    return ResumeBundle(**base)  # type: ignore[arg-type]


def test_flags_numbers_not_in_any_source() -> None:
    r = _resume(sections={"exp": r"\resumeItem{Cut costs 42\% in 2019}"})
    # 2019 is in the uploaded text, 42 is not -> only 42 is suspect
    assert unverified_numbers(r, ["joined in 2019"]) == ["42"]


def test_nothing_flagged_when_every_number_traces() -> None:
    r = _resume(sections={"exp": r"\resumeItem{Led 5 engineers since 2019}"})
    assert unverified_numbers(r, ["team of 5", "since 2019"]) == []


def test_number_from_a_user_answer_is_allowed() -> None:
    r = _resume(heading_tex="Expected CTC: 7 LPA")
    assert unverified_numbers(r, ["", "7"]) == []


def test_no_numbers_means_nothing_to_flag() -> None:
    r = _resume(sections={"skills": "Python, Go"}, project_blocks={"a": r"\resumeItem{Built it}"})
    assert unverified_numbers(r, []) == []


def test_commas_and_scale_are_normalized() -> None:
    r = _resume(sections={"exp": "Served 10,000 users"})
    assert unverified_numbers(r, ["10000 MAU"]) == []
