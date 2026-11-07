"""Replay real application logs into a separate case-review workspace, without app calls."""

import argparse
import json
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient

from evals_app.api import create_app
from evals_app.logs import load_trials
from evals_app.models import Plan, fingerprint, identifier
from evals_app.validation import validate_execution

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--experiment", required=True)
    args = parser.parse_args()
    source_path = args.workspace.resolve() / "state.json"
    snapshot = json.loads(source_path.read_text("utf-8"))
    original = snapshot["experiments"][args.experiment]
    plan = Plan.model_validate(original["plan"])
    assert original["status"] == "completed" and plan.application.provenance == "real"
    failed = []
    for key, run in original["runs"].items():
        trials = load_trials(run)
        split = key.rsplit(":", 1)[1]
        validate_execution(
            trials,
            [case for case in plan.cases if case.split == split],
            plan.application,
            original["candidates"][run["candidate_id"]]["config"],
            run["candidate_id"],
            plan.epochs,
            log=run["log"],
            grader=plan.semantic_grader,
        )
        failed.extend(trial for trial in trials if any(j.status == "fail" for j in trial.judgments))
    assert failed, "No native failed samples to review"
    source_trial = failed[0]
    source_case = next(case for case in plan.cases if case.id == source_trial.sample_id)
    reviewed = source_case.model_copy(
        update={
            "id": "reviewed-generated-regression",
            "group": "reviewed-regression-group",
            "split": "counterexample",
            "input": f"Generated regression of {source_case.id}",
        }
    )
    directory = ROOT / "artifacts" / identifier("case-reuse-acceptance")
    app = create_app(directory / "workspace", background=False)
    service = app.state.evals_service
    result = {
        "source": str(source_path),
        "source_experiment": args.experiment,
        "source_fingerprint": fingerprint(original),
        "application_calls": 0,
    }
    with TestClient(app, base_url="http://127.0.0.1:7575") as client:

        def seed(state):
            state["applications"][plan.application.id] = deepcopy(
                snapshot["applications"][plan.application.id]
            )
            state["experiments"][args.experiment] = deepcopy(original)
            state["calibrations"] = deepcopy(snapshot["calibrations"])

        service.store.update(seed)
        body = {
            "experiment_id": args.experiment,
            "sample_id": source_case.id,
            "case": reviewed.model_dump(mode="json"),
            "redaction_note": (
                "Reviewed generated isolation data; no production or user content. "
                "Preserve only test task and rubric metadata."
            ),
        }
        registered = client.post(
            "/api/evals/regression-cases", json=body, headers={"Idempotency-Key": "review"}
        )
        assert registered.status_code == 200, registered.text
        record = registered.json()
        assert source_trial.log in record["source_logs"]
        assert (
            client.post(
                "/api/evals/regression-cases", json=body, headers={"Idempotency-Key": "review"}
            ).json()
            == record
        )
        duplicate = client.post(
            "/api/evals/regression-cases", json=body, headers={"Idempotency-Key": "duplicate"}
        )
        assert duplicate.status_code == 409, duplicate.text
        bad = {**body, "redaction_note": " "}
        assert (
            client.post(
                "/api/evals/regression-cases", json=bad, headers={"Idempotency-Key": "blank"}
            ).status_code
            == 422
        )
        future = plan.model_dump(mode="json")
        future["application"] = snapshot["applications"][plan.application.id]["declaration"]
        future["dataset_version"] = "reviewed-regression-new-data-v2"
        train = reviewed.model_copy(
            update={
                "split": "train",
                "metadata": {**reviewed.metadata, "regression_case_id": record["id"]},
            }
        )
        future["cases"][0] = train.model_dump(mode="json")
        stale = client.post(
            "/api/evals/experiments",
            json={"plan": future, "use_gepa": False},
            headers={"Idempotency-Key": "stale-holdout"},
        )
        assert stale.status_code == 422 and "independent terminal holdout" in stale.text, stale.text
        for case in future["cases"]:
            if case["split"] == "holdout":
                case.update(
                    id=f"new-{case['id']}",
                    group=f"new-{case['group']}",
                    input=f"New independent {case['input']}",
                    metadata={
                        **case["metadata"],
                        "number": int(case["metadata"]["number"]) + 100000,
                    },
                )
        prepared = client.post(
            "/api/evals/experiments",
            json={"plan": future, "use_gepa": False},
            headers={"Idempotency-Key": "fresh-holdout"},
        )
        assert prepared.status_code == 200, prepared.text
        created = prepared.json()
        assert service.store.read()["experiments"][created["id"]]["regression_sources"] == [
            record["id"]
        ]
        # Admission is tested here; new business cases are not executed or scored.
        started = client.post(
            f"/api/evals/experiments/{created['id']}/start",
            json={"fingerprint": created["fingerprint"]},
        )
        assert started.status_code == 200, started.text
        service.control(created["id"], "cancel")
        result.update(
            status="passed",
            reviewed_case=record,
            duplicate_status=duplicate.status_code,
            stale_holdout_status=stale.status_code,
            independent_plan=created,
            execution="new plan admitted, queued and cancelled; no quality conclusion",
        )
    (directory / "result.json").write_text(json.dumps(result, indent=2), "utf-8")
    (ROOT / "artifacts/case-reuse-current.txt").write_text(str(directory), "utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
