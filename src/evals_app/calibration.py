"""Executable calibration of the deterministic rule interpreter and analyzer."""

from typing import Any

from evals_app.analysis import findings
from evals_app.fixtures import fixture_application, fixture_plan
from evals_app.models import (
    Application,
    Case,
    Evidence,
    Observation,
    Status,
    fingerprint,
    identifier,
    now,
)
from evals_app.scoring import coverage, judge


def calibrate() -> dict:
    app = fixture_application()
    case = fixture_plan().cases[0]
    reference = Evidence(
        source="fixture-verifier",
        locator="result.success",
        version="v1",
        scope="shareable",
        collected_at=now(),
    )
    observed = Observation(
        output="self reported success",
        application_version=app.version,
        facts={"result": {"success": False}},
        evidence={"result.success": reference},
    )
    failed = judge(case, app, observed)
    passed = judge(case, app, observed.model_copy(update={"facts": {"result": {"success": True}}}))
    missing = judge(case, app, None)
    no_rules = judge(case.model_copy(update={"rules": []}), app, observed)
    broken_rule = case.rules[0].model_copy(update={"operator": "lte", "expected": 1})
    invalid = judge(
        case.model_copy(update={"rules": [broken_rule]}),
        app,
        observed.model_copy(update={"facts": {"result": {"success": "not-numeric"}}}),
    )
    checks = [
        failed[0].status == Status.FAIL,
        passed[0].status == Status.PASS,
        missing[0].status == Status.UNVERIFIABLE,
        all(j.status == Status.NOT_APPLICABLE for j in no_rules),
        invalid[0].status == Status.GRADING_ERROR,
        coverage(missing)["confirmed_pass_ratio"] == 0,
        coverage(no_rules)["coverage"] is None,
        analyzer_calibration()["correct"] == analyzer_calibration()["expected"],
    ]
    return {
        "scorer_version": "rules-v1",
        "analyzer_version": "scout-rules-v1",
        "dataset_version": "calibration-v1",
        "expected": len(checks),
        "correct": sum(checks),
        "checks": checks,
        "analyzer_calibration": analyzer_calibration(),
        "scope": "Rule interpreter and analyzer; application rubrics require calibration",
    }


def analyzer_calibration() -> dict[str, Any]:
    """Independent, labeled positive/negative/boundary checks for local scanners."""
    call = {"name": "lookup", "arguments": {"id": 1}}
    examples = [
        ("duplicate-input", {"process": {"calls": [call, {**call, "id": "new"}]}}, 1),
        ("different-input", {"process": {"calls": [call, {**call, "arguments": {"id": 2}}]}}, 0),
        ("names-only", {"process": {"calls": ["lookup", "lookup"]}}, 0),
        ("unchanged-retry", {"process": {"calls": [{**call, "error": "denied"}, call]}}, 2),
        ("early-stop", {"process": {"terminated": True, "required_steps_complete": False}}, 1),
        ("required-ref", {"evidence": {"required": True, "citations": []}}, 1),
        ("optional-ref", {"evidence": {"citations": []}}, 0),
        ("unsupported", {"evidence": {"supported": False}}, 1),
        ("context-lost", {"retrieval": {"recalled": ["doc"], "context": []}}, 1),
        (
            "required-lost-in-rerank",
            {
                "retrieval": {
                    "required": ["expiry"],
                    "recalled": ["expiry", "policy"],
                    "reranked": ["policy"],
                    "context": ["policy"],
                }
            },
            1,
        ),
        (
            "required-lost-in-context",
            {
                "retrieval": {
                    "required": ["expiry"],
                    "recalled": ["expiry", "policy"],
                    "reranked": ["expiry", "policy"],
                    "context": ["policy"],
                }
            },
            1,
        ),
        ("unlabeled-partial", {"retrieval": {"recalled": ["a", "b"], "context": ["b"]}}, 0),
        ("unexposed-stages", {}, 0),
    ]
    details = [
        {"sample": name, "expected": count, "actual": len(findings({"facts": facts}))}
        for name, facts, count in examples
    ]
    return {
        "analyzer_version": "scout-rules-v1",
        "dataset_version": "analyzer-calibration-v3",
        "expected": len(details),
        "correct": sum(row["expected"] == row["actual"] for row in details),
        "details": details,
        "scope": "Deterministic trace findings; hypotheses are not verified causes",
    }


def calibrate_business(
    application: Application,
    annotations: list[dict[str, Any]],
    dataset_version: str,
    minimum_accuracy: float,
) -> dict[str, Any]:
    if len(annotations) < 3 or not 0 < minimum_accuracy <= 1:
        raise ValueError("Calibration requires positive, negative and missing-evidence examples")
    expected, correct = 0, 0
    labels = set()
    details = []
    rule_hashes = set()
    for row in annotations:
        case = Case.model_validate(row["case"])
        observation = Observation.model_validate(row["observation"]) if row["observation"] else None
        judgments = {j.dimension.value: j for j in judge(case, application, observation)}
        for dimension, label in row["expected"].items():
            rule = next((r for r in case.rules if r.dimension.value == dimension), None)
            if rule is None or rule.operator == "semantic":
                raise ValueError("Calibration label must name a planned business rule")
            rule_hashes.add(fingerprint(rule))
            label = Status(label)
            labels.add(label)
            actual = judgments[dimension].status
            expected += 1
            correct += actual == label
            details.append(
                {
                    "sample_id": case.id,
                    "dimension": dimension,
                    "expected": label.value,
                    "actual": actual.value,
                }
            )
    if not {Status.PASS, Status.FAIL, Status.UNVERIFIABLE}.issubset(labels):
        raise ValueError("Calibration labels must cover pass, fail and unverifiable")
    if not expected or correct / expected < minimum_accuracy:
        raise ValueError(
            "Application business scorer did not reach its declared calibration target"
        )
    calibration_id = identifier("calibration")
    return {
        "application_id": application.id,
        "rule_fingerprints": sorted(rule_hashes),
        "details": details,
        "calibration": {
            "scorer_version": "rules-v1",
            "dataset_version": dataset_version,
            "expected": expected,
            "correct": correct,
            "minimum_accuracy": minimum_accuracy,
            "artifact": f"registered:{calibration_id}",
        },
    }
