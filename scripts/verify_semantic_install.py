"""Run with the installed Python (-I), using generated independent fixture files."""

import json
import sys
from pathlib import Path

from inspect_ai.log import read_eval_log

from evals_app.models import Plan
from evals_app.semantic import SemanticValidationCase, schedule_calibration
from evals_app.service import Service


def main() -> None:
    directory, manifest_path, labels_path = (Path(arg).resolve() for arg in sys.argv[1:])
    plan = Plan.model_validate(json.loads(manifest_path.read_text("utf-8"))["plan"])
    cases = [
        SemanticValidationCase.model_validate(case)
        for case in json.loads(labels_path.read_text("utf-8"))["cases"]
    ]
    definition = plan.semantic_grader or plan.semantic_analyzer
    assert definition is not None
    grading = plan.semantic_grader is not None
    role = "grading" if grading else "analysis"
    collection = "grader_calibrations" if grading else "analyzer_calibrations"
    service = Service(directory, background=False)
    try:
        service.register(plan.application, key="app")
        created = service.create(plan, [], key="installed-semantic", use_gepa=False)
        schedule_calibration(service, created["id"], cases, key="labels", role=role)
        service.run(created["id"])
        record = service.store.read()[collection][definition.calibration_id]
        assert record["status"] == "passed", record
        service.start(created["id"], created["fingerprint"])
        service.run(created["id"])
        experiment = service.store.read()["experiments"][created["id"]]
        assert experiment["status"] == "completed", experiment.get("reason")
        if grading:
            grades = [
                score.metadata["semantic_grade"]
                for run in experiment["runs"].values()
                for sample in read_eval_log(run["log"]).samples or []
                for score in (sample.scores or {}).values()
            ]
            assert {grade["status"] for grade in grades} == {"pass", "fail"}
            assert all(
                grade["calibration_id"] == definition.calibration_id
                and grade["result"]["references"]
                for grade in grades
            )
            evidence = {"native_semantic_grades": grades}
        else:
            findings = [finding for finding in experiment["findings"] if finding.get("model")]
            assert findings and all(finding["scanner_references"] for finding in findings)
            evidence = {"semantic_findings": findings}
        result = {
            "experiment_id": created["id"],
            "build": experiment["build"],
            "status": experiment["status"],
            "calibration": record,
            **evidence,
            "ledger": experiment["ledger"],
            "provenance": "fixture; no semantic quality claim",
        }
        (directory / "installation.json").write_text(json.dumps(result, indent=2), "utf-8")
        print(json.dumps({"status": result["status"], "calibration": record["status"]}))
    finally:
        service.close()


if __name__ == "__main__":
    main()
