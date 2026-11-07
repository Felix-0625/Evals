"""Evidence-bound deterministic business judgments and explicit denominators."""

import math
from collections import Counter
from collections.abc import Mapping
from typing import Any

from evals_app.models import Application, Case, Dimension, Judgment, Observation, Status


def resolve(facts: dict[str, Any], path: str) -> Any:
    value: Any = facts
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(path)
        value = value[part]
    return value


def equal(actual: Any, expected: Any) -> bool:
    """JSON booleans are distinct from numbers, including inside composite states."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            equal(actual[k], v) for k, v in expected.items()
        )
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(
            equal(a, e) for a, e in zip(actual, expected, strict=True)
        )
    return actual == expected


def shareable_paths(scopes: Mapping[str, str]) -> set[str]:
    """Withhold compound paths that overlap explicitly local evidence."""
    local = {path for path, scope in scopes.items() if scope != "shareable"}
    return {
        path
        for path, scope in scopes.items()
        if scope == "shareable"
        and not any(
            path.startswith(private + ".") or private.startswith(path + ".") for private in local
        )
    }


def assertion(operator: str, value: Any, expected: Any) -> bool:
    if operator == "eq":
        return equal(value, expected)
    if operator == "contains":
        if isinstance(value, str) and isinstance(expected, str):
            return expected in value
        if isinstance(value, (list, dict)):
            return any(equal(member, expected) for member in value)
        raise TypeError("contains requires text, a list or an object")
    if operator == "subset":
        if not isinstance(value, list) or not isinstance(expected, list):
            raise TypeError("subset requires two lists")
        return all(any(equal(member, item) for member in value) for item in expected)
    if operator not in {"lte", "gte"}:
        raise ValueError("Unsupported deterministic assertion")
    if type(value) not in (int, float) or type(expected) not in (int, float):
        raise TypeError("Numeric comparison requires numbers")
    if not math.isfinite(value) or not math.isfinite(expected):
        raise ValueError("Numeric comparison requires finite numbers")
    return value <= expected if operator == "lte" else value >= expected


def judge(case: Case, application: Application, observation: Observation | None) -> list[Judgment]:
    rules = {rule.dimension: rule for rule in case.rules}
    results = []
    for dimension in Dimension:
        rule = rules.get(dimension)
        status, reason, evidence = Status.NOT_APPLICABLE, "No planned business assertion", []
        critical = rule.critical if rule else False
        if rule:
            status, reason = Status.UNVERIFIABLE, "Required evidence is unavailable"
            if dimension not in application.observable:
                reason = "Application does not expose this planned dimension"
            elif rule.operator == "semantic":
                reason = "Calibrated semantic judgment is unavailable"
            elif observation is not None:
                reference = observation.evidence.get(rule.path)
                if reference and reference.source == rule.source and reference.version:
                    try:
                        value = resolve(observation.facts, rule.path)
                        passed = assertion(rule.operator, value, rule.expected)
                        status = Status.PASS if passed else Status.FAIL
                        reason = (
                            f"{rule.path} {rule.operator} {rule.expected!r}; observed {value!r}"
                        )
                        evidence = [reference]
                    except KeyError:
                        reason = f"Observation missing: {rule.path}"
                    except (TypeError, ValueError, OverflowError) as error:
                        status, reason = Status.GRADING_ERROR, f"Invalid assertion data: {error}"
        results.append(
            Judgment(
                dimension=dimension,
                status=status,
                reason=reason,
                evidence=evidence,
                critical=critical,
            )
        )
    return results


def coverage(judgments: list[Judgment]) -> dict[str, Any]:
    counts = Counter(j.status.value for j in judgments)
    applicable = len(judgments) - counts[Status.NOT_APPLICABLE]
    valid = counts[Status.PASS] + counts[Status.FAIL]
    return {
        "counts": {s.value: counts[s.value] for s in Status},
        "applicable": applicable,
        "confirmed_pass_ratio": counts[Status.PASS] / applicable if applicable else None,
        "valid_pass_rate": counts[Status.PASS] / valid if valid else None,
        "coverage": valid / applicable if applicable else None,
    }
