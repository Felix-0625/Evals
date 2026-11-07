"""Experiment cancellation across native evaluation, analysis and reflection."""

import os
import threading
import time

import pytest

from evals_app.fixtures import fixture_plan
from evals_app.models import Plan
from evals_app.service import Service


def test_cancel_during_native_evaluation_does_not_advance_experiment(tmp_path):
    raw = fixture_plan(groups=2).model_dump(mode="json")
    raw["epochs"] = 1
    for case in raw["cases"]:
        if case["split"] == "train":
            case["metadata"]["delay"] = 60
    plan = Plan.model_validate(raw)
    service = Service(tmp_path, background=False)
    supervisor = None
    try:
        service.register(plan.application, key="app")
        created = service.create(plan, [], key="eval-cancel", use_gepa=True)
        experiment_id = created["id"]
        service.start(experiment_id, created["fingerprint"])
        supervisor = threading.Thread(target=service.run, args=(experiment_id,))
        supervisor.start()
        deadline = time.monotonic() + 45
        while True:
            state = service.store.experiment_fields(experiment_id, "status", "ledger", "reason")
            if state["ledger"]["application"]["reserved"] and service.runtime.discover(
                experiment_id
            ):
                break
            assert supervisor.is_alive(), state
            assert time.monotonic() < deadline, state
            time.sleep(0.05)
        process = service.runtime.processes[experiment_id]
        service.control(experiment_id, "cancel")
        supervisor.join(20)
        assert not supervisor.is_alive() and process.poll() is not None
        experiment = service.store.read()["experiments"][experiment_id]
        assert experiment["status"] == "cancelled"
        assert experiment["stage"] == "baseline"
        assert experiment["worker"] is None and "native_cancel" in experiment
        assert not experiment["runs"] and "gepa" not in experiment
    finally:
        if supervisor and supervisor.is_alive():
            service.control(experiment_id, "cancel")
            supervisor.join(20)
        service.close()


@pytest.mark.parametrize("phase", ["analysis", "reflection"])
def test_cancel_does_not_dispatch_a_subsequent_candidate(tmp_path, monkeypatch, phase):
    raw = fixture_plan(groups=2).model_dump(mode="json")
    raw["text_proposer"] = "cancellation_plugin:propose"
    plan = Plan.model_validate(raw)
    marker = tmp_path / "entered"
    (tmp_path / "cancellation_plugin.py").write_text(
        "import time\nfrom pathlib import Path\n"
        "def propose(payload):\n"
        " Path(payload['marker']).write_text('ready')\n"
        " time.sleep(120)\n"
        " return {'changes': {'prompt': 'grounded'}, 'cost': 0}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    service = Service(tmp_path / "workspace", background=False)
    entered = threading.Event()
    release = threading.Event()
    evaluations = []
    proposed = []

    def evaluate(experiment_id, frozen, candidate, split):
        evaluations.append((candidate["id"], split))
        # No application execution is needed to isolate these two control boundaries.
        return (
            []
            if phase == "reflection"
            else [
                type(
                    "Trace",
                    (),
                    {"sample_id": "train", "epoch": 1, "log": "test.eval", "observation": None},
                )()
            ]
        )

    def analyze(observation):
        entered.set()
        assert release.wait(20)
        return []

    def search(runtime, experiment_id, frozen, directory):
        runtime.reflect(experiment_id, frozen, {"marker": str(marker)})
        proposed.append("child")
        return {}

    monkeypatch.setattr(service, "_evaluate", evaluate)
    monkeypatch.setattr("evals_app.service.search", search)
    if phase == "analysis":
        monkeypatch.setattr("evals_app.service.findings", analyze)
    thread = None
    try:
        service.register(plan.application, key="app")
        created = service.create(plan, [], key="experiment")
        service.start(created["id"], created["fingerprint"])
        thread = threading.Thread(target=service.run, args=(created["id"],))
        thread.start()
        deadline = time.monotonic() + 20
        while not (entered.is_set() if phase == "analysis" else marker.exists()):
            assert thread.is_alive(), service.store.read()["experiments"][created["id"]]
            assert time.monotonic() < deadline, "Target phase did not become active"
            time.sleep(0.05)
        process = service.runtime.processes.get(created["id"])
        service.control(created["id"], "cancel")
        release.set()
        thread.join(15)
        assert not thread.is_alive()
        experiment = service.store.read()["experiments"][created["id"]]
        assert experiment["status"] == "cancelled"
        assert evaluations == [("baseline", "train"), ("baseline", "selection")]
        assert proposed == [] and "gepa" not in experiment
        if phase == "reflection":
            assert process is not None and process.poll() is not None
            assert experiment["ledger"]["reflection"]["unknown"] == 1
            assert experiment["worker"] is None
    finally:
        release.set()
        if thread and thread.is_alive():
            service.control(created["id"], "cancel")
            thread.join(15)
        service.close()


def test_unknown_reflection_cost_blocks_candidate_and_future_calls(tmp_path, monkeypatch):
    (tmp_path / "unknown_cost_plugin.py").write_text(
        "def propose(payload):\n return {'changes': {'prompt': 'grounded'}}\n", "utf-8"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    plan = fixture_plan(groups=2).model_copy(
        update={"text_proposer": "unknown_cost_plugin:propose"}
    )
    service = Service(tmp_path / "workspace", background=False)
    try:
        service.register(plan.application, key="app")
        created = service.create(plan, [], key="unknown-cost", use_gepa=False)
        with pytest.raises(ValueError, match="cost is unknown"):
            service.runtime.reflect(created["id"], plan, {})
        experiment = service.store.read()["experiments"][created["id"]]
        assert experiment["ledger"]["reflection"]["unknown"] == 1
        assert experiment["worker"] is None
        assert list(experiment["candidates"]) == ["baseline"]
        from evals_app.runtime import BudgetExceeded

        with pytest.raises(BudgetExceeded, match="unknown cost"):
            service.runtime.reserve(created["id"], "reflection", 0)
    finally:
        service.close()
