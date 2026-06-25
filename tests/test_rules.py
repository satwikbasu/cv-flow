"""Declarative crux-field exclusion rules."""

from cvflow.discovery.rules import crux_excluded

CRUX = {
    "night_shift_only": False,
    "app_maintenance_focus": True,
    "min_years_required": 5,
    "red_flags": ["vague"],
}


def test_equals_rule_matches():
    excluded, reason = crux_excluded(CRUX, [{"field": "app_maintenance_focus", "equals": True}])
    assert excluded is True
    assert "app_maintenance_focus" in reason


def test_greater_than_rule_matches():
    excluded, _ = crux_excluded(CRUX, [{"field": "min_years_required", "greater_than": 3}])
    assert excluded is True


def test_contains_any_rule():
    excluded, _ = crux_excluded(
        {"red_flags": ["unpaid"]}, [{"field": "red_flags", "contains_any": ["unpaid", "scam"]}]
    )
    assert excluded is True


def test_contains_any_matches_scalar_field():
    # seniority gate: a scalar 'senior' is matched by contains_any [senior, lead]
    rule = [{"field": "seniority_signal", "contains_any": ["senior", "lead"]}]
    assert crux_excluded({"seniority_signal": "senior"}, rule)[0] is True
    assert crux_excluded({"seniority_signal": "lead"}, rule)[0] is True
    assert crux_excluded({"seniority_signal": "junior"}, rule)[0] is False


def test_no_rule_matches_returns_false():
    excluded, reason = crux_excluded(CRUX, [{"field": "night_shift_only", "equals": True}])
    assert excluded is False
    assert reason is None


def test_null_field_never_matches():
    # min_years_required unknown -> a greater_than rule must NOT drop it (never fabricate)
    excluded, _ = crux_excluded(
        {"min_years_required": None}, [{"field": "min_years_required", "greater_than": 3}]
    )
    assert excluded is False
