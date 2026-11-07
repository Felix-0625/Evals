"""Write a no-provider fixture manifest and separate Scout calibration labels."""

import argparse
import json
from pathlib import Path

from evals_app.fixtures import fixture_plan
from evals_app.models import Evidence, Plan, SemanticAnalyzer, now
from evals_app.semantic import SemanticValidationCase


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--grader", action="store_true")
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    analyzer = SemanticAnalyzer(
        id="support-check",
        version="v1",
        question="Does the evidence suggest an unsupported claim?",
        evidence_paths=["evidence.supported"],
        model="mockllm/model",
        model_version="mockllm",
        estimate_per_call=0,
        pricing_version="fixture-zero-v1",
        cost_source="unbilled mock provider",
        calibration_id="support-calibration-v1",
        calibration_dataset_version="separate-labels-v1",
        fixture_replies=[
            "Evidence [M1] suggests a problem.\nANSWER: yes",
            "No problem.\nANSWER: no",
        ],
    )
    plan = fixture_plan(groups=2).model_copy(update={"epochs": 1, "semantic_analyzer": analyzer})
    if args.grader:
        raw = plan.model_dump(mode="json")
        raw["semantic_analyzer"] = None
        raw["semantic_grader"] = analyzer.model_copy(
            update={
                "id": "grounding-grade",
                "calibration_id": "business-grade-v1",
                "fixture_replies": [
                    "Contradiction at [M1].\nANSWER: yes",
                    "No contradiction at [M1].\nANSWER: no",
                ],
            }
        ).model_dump(mode="json")
        for case in raw["cases"]:
            next(rule for rule in case["rules"] if rule["dimension"] == "evidence").update(
                operator="semantic", expected="no"
            )
        plan = Plan.model_validate(raw)
    labels = [
        SemanticValidationCase(
            id=f"label-{index}",
            group=f"independent-calibration-{index}",
            text=text,
            expected=expected,
            reference=Evidence(
                source="fixture-annotator",
                locator=f"separate-labels#{index}",
                version="v1",
                scope="local",
                collected_at=now(),
            ),
        ).model_dump(mode="json")
        for index, (text, expected) in enumerate(
            [
                ("The claim contradicts its source", "yes"),
                ("The claim agrees with the authorized source", "no"),
                (None, "unverifiable"),
            ]
        )
    ]
    for name, value in (
        ("manifest.json", {"plan": plan.model_dump(mode="json"), "use_gepa": False}),
        ("calibration.json", {"cases": labels}),
    ):
        path = directory / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
        print(path)


if __name__ == "__main__":
    main()
