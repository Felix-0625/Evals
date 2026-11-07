import pytest
from pydantic import ValidationError

from evals_app.fixtures import fixture_plan
from evals_app.models import Evidence, Observation, Status, now
from evals_app.scoring import equal, judge


@pytest.mark.parametrize(
    "field,value",
    [("locator", ""), ("version", " "), ("source", ""), ("collected_at", "2026-11-06")],
)
def test_unlocated_or_undated_evidence_is_rejected(field, value):
    raw = {
        "source": "fixture-verifier",
        "locator": "artifact#state",
        "version": "v1",
        "scope": "local",
        "collected_at": now(),
    }
    raw[field] = value
    with pytest.raises(ValidationError):
        Evidence.model_validate(raw)


def test_boolean_business_state_cannot_pass_as_a_number():
    plan = fixture_plan()
    reference = Evidence(
        source="fixture-verifier",
        locator="artifact#state",
        version="v1",
        scope="local",
        collected_at=now(),
    )
    observation = Observation(
        output="success",
        application_version=plan.application.version,
        facts={"result": {"success": 1}},
        evidence={"result.success": reference},
    )
    assert judge(plan.cases[0], plan.application, observation)[0].status == Status.FAIL
    assert not equal({"authorized": [True]}, {"authorized": [1]})
    assert equal({"amount": 1.0}, {"amount": 1})


@pytest.mark.parametrize(
    ("operator", "expected", "actual", "status"),
    [
        ("contains", True, [1], Status.FAIL),
        ("subset", [True], [1], Status.FAIL),
        ("subset", ["a"], "abc", Status.GRADING_ERROR),
        ("lte", 1, True, Status.GRADING_ERROR),
        ("gte", 1, "Infinity", Status.GRADING_ERROR),
        ("subset", [{"id": 1}], [{"id": 1}, {"id": 2}], Status.PASS),
    ],
)
def test_structured_assertions_cannot_pass_coerced_types(operator, expected, actual, status):
    plan = fixture_plan()
    case = plan.cases[0]
    rule = case.rules[0].model_copy(update={"operator": operator, "expected": expected})
    case = case.model_copy(update={"rules": [rule]})
    observation = Observation(
        output="observed",
        application_version=plan.application.version,
        facts={"result": {"success": actual}},
        evidence={
            "result.success": Evidence(
                source=rule.source,
                locator="artifact#state",
                version="v1",
                scope="local",
                collected_at=now(),
            )
        },
    )
    assert judge(case, plan.application, observation)[0].status == status
