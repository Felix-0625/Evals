"""Apply and roll back a recommended version in the actual isolated application gateway."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from acceptance_applications import application_server, request_observation

from evals_app.calibration import calibrate_business
from evals_app.models import Case, Plan, identifier
from evals_app.monitoring import observe
from evals_app.service import Service


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["vera", "nexrag"], required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--acceptance", type=Path, required=True)
    parser.add_argument("--paired-control", action="store_true")
    args = parser.parse_args()
    directory = args.acceptance.resolve()
    snapshot = json.loads((directory / "workspace/state.json").read_text("utf-8"))
    original = next(e for e in snapshot["experiments"].values() if e.get("reports"))
    plan = Plan.model_validate(original["plan"])
    port = urlparse(plan.application.entrypoint).port
    lifecycle_dir = directory / identifier("lifecycle")
    lifecycle_dir.mkdir()
    with application_server(args.kind, args.source.resolve(), lifecycle_dir, port=port) as (
        url,
        version,
        client,
        token,
    ):
        if version != plan.application.version:
            raise RuntimeError("Source baseline changed since independent verification")
        service = Service(directory / "workspace", background=False)
        try:
            candidate_id = original["selected_candidate"]
            run_id = identifier("adoption")
            requested = f"{version}+{run_id}"
            operation = service.operation(
                original["id"], candidate_id, version, requested, key=run_id
            )
            headers = {"Authorization": f"Bearer {token}"}
            assert service.store.read()["applications"][plan.application.id]["version"] == version
            response = client.post(url + "/operations", headers=headers, json=operation)
            response.raise_for_status()
            actual = client.get(url + "/effective", headers=headers)
            actual.raise_for_status()
            actual = actual.json()
            receipt_args = {
                "executor": "local",
                "actual_version": actual["version"],
                "config_fingerprint": actual["config_fingerprint"],
                "status": "applied",
                "evidence": actual["evidence"],
                "key": run_id,
            }
            receipt = service.receipt(operation["id"], **receipt_args)
            assert service.receipt(operation["id"], **receipt_args) == receipt
            raw = plan.model_dump(mode="json")
            raw["application"] = service.store.read()["applications"][plan.application.id][
                "declaration"
            ]
            raw["dataset_version"] = f"new-tasks-{run_id}"
            number_offset = 1000 + max(
                (
                    case["metadata"].get("number", 0)
                    for experiment in snapshot["experiments"].values()
                    for case in experiment["plan"]["cases"]
                ),
                default=0,
            )
            raw["cases"] = [
                Case.model_validate(
                    {
                        **case.model_dump(mode="json"),
                        "id": f"new-{case.id}",
                        "group": f"{run_id}-{case.group}",
                        "input": f"New isolated follow-up {run_id}: {case.input}",
                        "metadata": {
                            **case.metadata,
                            "number": case.metadata["number"] + number_offset,
                        },
                    }
                ).model_dump(mode="json")
                for case in plan.cases
            ]
            new_plan = Plan.model_validate(raw)
            # Recalibrate the effective version; the old artifact may predate rule coverage checks.
            example = next(c for c in new_plan.cases if not c.metadata.get("attack"))
            positive = request_observation(client, new_plan.application, example, token)
            negative = request_observation(
                client, new_plan.application, example, token, plan.application.config
            )
            annotations = [
                {
                    "case": example.model_dump(mode="json"),
                    "observation": value,
                    "expected": {
                        rule.dimension.value: (
                            "unverifiable"
                            if value is None
                            else "fail"
                            if label == "fail"
                            and rule.dimension.value
                            in ({"result"} if args.kind == "vera" else {"result", "retrieval"})
                            else "pass"
                        )
                        for rule in example.rules
                    },
                }
                for value, label in ((positive, "pass"), (negative, "fail"), (None, "unverifiable"))
            ]
            calibration = calibrate_business(
                new_plan.application, annotations, f"effective-calibration-{run_id}", 1
            )
            service.store.update(
                lambda state: state["calibrations"].update(
                    {calibration["calibration"]["artifact"]: calibration}
                )
            )
            raw["calibration"] = calibration["calibration"]
            new_plan = Plan.model_validate(raw)
            observation = observe(
                service, operation["id"], new_plan, key=run_id, compare_previous=args.paired_control
            )
            service.run(observation["id"])
            monitored = service.store.read()["observations"].get(observation["id"])
            if not monitored or monitored["hard_failures"]:
                raise RuntimeError("New-task observation failed; inspect the preserved experiment")
            rollback = service.operation(
                original["id"],
                candidate_id,
                requested,
                version,
                key=f"rollback-{run_id}",
                rollback=operation["id"],
            )
            response = client.post(url + "/operations", headers=headers, json=rollback)
            response.raise_for_status()
            effective = client.get(url + "/effective", headers=headers).json()
            service.receipt(
                rollback["id"],
                executor="local",
                actual_version=effective["version"],
                config_fingerprint=effective["config_fingerprint"],
                status="applied",
                evidence=effective["evidence"],
                key=f"rollback-{run_id}",
            )
            assert (
                effective["config"] == plan.application.config and effective["version"] == version
            )
            result = {
                "application": args.kind,
                "adoption": receipt,
                "observation": monitored,
                "rollback": service.store.read()["operations"][rollback["id"]],
                "boundary": (
                    "Isolated configuration lifecycle and generated follow-up tasks; "
                    "no production causal claim"
                ),
            }
            (lifecycle_dir / "result.json").write_text(json.dumps(result, indent=2), "utf-8")
            print(
                json.dumps(
                    {
                        "application": args.kind,
                        "result": str(lifecycle_dir / "result.json"),
                        "adoption": "applied",
                        "new_task_observation": monitored["summary"]["rounds"],
                        "rollback": "applied",
                        "configuration_validation": monitored.get("validation"),
                    },
                    indent=2,
                )
            )
            if args.paired_control and monitored["validation"]["decision"] != "recommend":
                raise RuntimeError("New-task paired configuration gain was not confirmed")
        finally:
            service.close()


if __name__ == "__main__":
    main()
