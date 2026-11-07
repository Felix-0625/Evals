"""Drive the product HTTP requeue endpoint against an actual native sample error."""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from inspect_ai.log import read_eval_log

from evals_app.api import create_app
from evals_app.fixtures import fixture_plan
from evals_app.models import identifier

ROOT = Path(__file__).resolve().parents[1]


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--verify":
        directory = Path(sys.argv[2]).resolve()
        result = json.loads((directory / "result.json").read_text("utf-8"))
        state = json.loads((directory / "workspace/state.json").read_text("utf-8"))
        assert len(state["experiments"]) == 1
        experiment = next(iter(state["experiments"].values()))
        assert experiment["status"] == "completed"
        assert result["accepted"]["changed"] and not result["duplicate"]["changed"]
        assert result["foreign_status"] == 422
        recovered = []
        for run in experiment["runs"].values():
            for sample in read_eval_log(run["log"]).samples or []:
                if sample.id == "train-0":
                    assert sample.error is None and sample.error_retries
                    errors = [item.message for item in sample.error_retries]
                    assert any(
                        "EVALS_ACCEPTANCE_TRANSIENT_SOLVER_FAILURE" in item for item in errors
                    )
                    recovered.append(
                        {
                            "log": run["log"],
                            "sample_id": sample.id,
                            "epoch": sample.epoch,
                            "prior_errors": errors,
                            "final_error": None,
                        }
                    )
        assert len(recovered) == 1
        result.update(
            experiment_id=experiment["id"], status=experiment["status"], recovered=recovered
        )
        (directory / "result.json").write_text(json.dumps(result, indent=2), "utf-8")
        print(json.dumps(result, indent=2))
        return
    directory = ROOT / "artifacts" / identifier("requeue-acceptance")
    directory.mkdir(parents=True)
    marker, held, release = (directory / name for name in ("failed", "held", "release"))
    real_popen = subprocess.Popen

    def launch(command, **kwargs):
        if len(command) == 4 and command[1:3] == ["-m", "evals_app.worker"]:
            command = [
                command[0],
                str(ROOT / "scripts/native_fault_worker.py"),
                command[3],
                str(marker),
                str(held),
                str(release),
            ]
        return real_popen(command, **kwargs)

    plan = fixture_plan(groups=2).model_copy(
        update={"epochs": 1, "environment": {"mode": "controlled native solver fault and requeue"}}
    )
    app = create_app(directory / "workspace", background=False)
    service = app.state.evals_service
    result = {"mode": "native solver error injection; fixture application; no quality claim"}
    thread = None
    with (
        TestClient(app, base_url="http://127.0.0.1:7575") as client,
        patch("evals_app.runtime.subprocess.Popen", side_effect=launch),
    ):
        try:
            service.register(plan.application, key="app")
            created = service.create(plan, [], key="native-error", use_gepa=False)
            service.start(created["id"], created["fingerprint"])
            thread = threading.Thread(target=service.run, args=(created["id"],))
            thread.start()
            deadline = time.monotonic() + 90
            while not held.exists():
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError(service.store.read()["experiments"][created["id"]])
                time.sleep(0.1)
            assert marker.exists()
            path = f"/api/evals/experiments/{created['id']}/samples/requeue"
            while True:
                try:
                    response = client.post(path, json={"sample_id": "train-0", "epoch": 1})
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError("Owned native requeue did not become available")
                time.sleep(0.2)
            result["accepted"] = response.json()
            assert result["accepted"]["changed"] and result["accepted"]["status"] == "error", result
            duplicate = client.post(path, json={"sample_id": "train-0", "epoch": 1})
            assert duplicate.status_code == 200, duplicate.text
            result["duplicate"] = duplicate.json()
            assert not result["duplicate"]["changed"], result
            foreign = client.post(path, json={"sample_id": "foreign", "epoch": 1})
            result["foreign_status"] = foreign.status_code
            assert foreign.status_code == 422, foreign.text
            release.write_text("resume held sample", "utf-8")
            thread.join(75)
            assert not thread.is_alive(), "Native run did not drain after recovery"
            experiment = service.store.read()["experiments"][created["id"]]
            assert experiment["status"] == "completed", experiment.get("reason")
            recovered = []
            for run in experiment["runs"].values():
                log = read_eval_log(run["log"])
                for sample in log.samples or []:
                    if sample.id == "train-0":
                        assert sample.error is None and sample.error_retries
                        errors = [item.message for item in sample.error_retries]
                        assert any(
                            "EVALS_ACCEPTANCE_TRANSIENT_SOLVER_FAILURE" in item for item in errors
                        )
                        recovered.append(
                            {
                                "log": run["log"],
                                "sample_id": sample.id,
                                "epoch": sample.epoch,
                                "prior_errors": errors,
                                "final_error": None,
                            }
                        )
            assert len(recovered) == 1
            result.update(
                experiment_id=created["id"], status=experiment["status"], recovered=recovered
            )
        finally:
            release.write_text("cleanup release", "utf-8")
            if thread and thread.is_alive():
                service.control(created["id"], "cancel")
                thread.join(20)
            (directory / "result.json").write_text(json.dumps(result, indent=2), "utf-8")
            (ROOT / "artifacts/requeue-current.txt").write_text(str(directory), "utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
