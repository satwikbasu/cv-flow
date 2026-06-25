"""Declarative, config-driven exclusion rules over crux fields.

A job is dropped if it matches any rule. A rule whose field is null/absent never matches —
we never drop on a fact we don't have. The operators are a small fixed set; the rules are
config, the evaluator is code.
"""

from __future__ import annotations

from typing import Any


def _rule_matches(value: Any, rule: dict[str, Any]) -> bool:
    if value is None:
        return False  # never drop on an absent fact
    if "equals" in rule:
        return bool(value == rule["equals"])
    if "greater_than" in rule:
        return isinstance(value, (int, float)) and value > rule["greater_than"]
    if "less_than" in rule:
        return isinstance(value, (int, float)) and value < rule["less_than"]
    if "contains_any" in rule:
        wanted = set(rule["contains_any"])
        haystack = value if isinstance(value, (list, set, tuple)) else [value]
        return bool(wanted.intersection(haystack))
    return False


def crux_excluded(
    crux: dict[str, Any], rules: list[dict[str, Any]]
) -> tuple[bool, str | None]:
    """Return (True, reason) if the crux matches any exclusion rule, else (False, None)."""
    for rule in rules:
        field = rule.get("field")
        if field is None:
            continue
        if _rule_matches(crux.get(field), rule):
            op = next(
                (k for k in ("equals", "greater_than", "less_than", "contains_any") if k in rule),
                "?",
            )
            return True, f"{field} {op} {rule.get(op)}"
    return False, None
