"""Paired configuration monitoring with explicit, controlled business outcomes."""

import anyio
import pytest

from evals_app.fixtures import fixture_plan, synthetic_application
from evals_app.models import Observation, Plan, Proposal, Trial, fingerprint, now
from evals_app.monitoring import observe
from evals_app.scoring import judge
from evals_app.service import Service
from evals_app.store import Conflict


@pytest.fixture
def adopted(tmp_path):
    service = Service(tmp_path, background=False)
    plan = fixture_plan(groups=2)
    service.register(plan.application, key="app")
    original = service.create(
        plan,
        [
            Proposal(
                changes={"reuse": True},
                source="manual",
                hypothesis="Lower cost",
                risk="Stale reads",
                expected_baseline=plan.application.version,
            )
        ],
        key="original",
    )
    candidate_id = next(
        k
        for k in service.store.read()["experiments"][original["id"]]["candidates"]
        if k != "baseline"
    )
    service.runtime.patch(
        original["id"],
        status="completed",
        reports={candidate_id: {"decision": "recommend", "baseline": plan.application.version}},
    )
    operation = service.operation(
        original["id"], candidate_id, plan.application.version, "v2", key="adopt"
    )
    service.receipt(
        operation["id"],
        executor="local",
        actual_version="v2",
        status="applied",
        config_fingerprint=operation["config_fingerprint"],
        key="receipt",
        evidence={
            "source": "unit state verifier",
            "locator": "effective#v2",
            "collected_at": now(),
        },
    )
    raw = plan.model_dump(mode="json")
    raw["application"] = service.store.read()["applications"][plan.application.id]["declaration"]
    raw["dataset_version"] = "fresh-tasks-v2"
    for case in raw["cases"]:
        for field in ("id", "group", "input"):
            case[field] = "new-" + case[field]
    yield service, operation, Plan.model_validate(raw)
    service.close()


def execute_records(service, plan, monkeypatch):
    def evaluate(experiment_id, frozen, candidate, split):
        rows = []
        for case in frozen.cases:
            if case.split != split:
                continue
            observation = Observation.model_validate(
                anyio.run(
                    synthetic_application,
                    {
                        "config": candidate["config"],
                        "metadata": case.metadata,
                        "application_version": frozen.application.version,
                    },
                )
            )
            for epoch in range(1, frozen.epochs + 1):
                rows.append(
                    Trial(
                        sample_id=case.id,
                        group=case.group,
                        epoch=epoch,
                        attempt_id=f"{case.id}-{epoch}",
                        candidate_id=candidate["id"],
                        run_id="unit-run",
                        config_fingerprint=fingerprint(candidate["config"]),
                        application_version=frozen.application.version,
                        observation=observation,
                        judgments=judge(case, frozen.application, observation),
                        attempts=[{"cost": observation.cost, "latency": 0.1}],
                        log="controlled.eval",
                    )
                )
        service.store.update(
            lambda s: s["experiments"][experiment_id]["runs"].update(
                {
                    f"{candidate['id']}:{split}": {
                        "trials": [r.model_dump(mode="json") for r in rows]
                    }
                }
            )
        )
        return rows

    monkeypatch.setattr(service, "_evaluate", evaluate)


@pytest.mark.parametrize("paired,decision", [(False, "insufficient_evidence"), (True, "recommend")])
def test_descriptive_success_is_separate_from_confirmed_configuration_gain(
    adopted, monkeypatch, paired, decision
):
    service, operation, plan = adopted
    created = observe(service, operation["id"], plan, key="new", compare_previous=paired)
    assert observe(service, operation["id"], plan, key="new", compare_previous=paired) == created
    with pytest.raises(Conflict):
        observe(service, operation["id"], plan, key="new", compare_previous=not paired)
    execute_records(service, plan, monkeypatch)
    service.run(created["id"])
    result = service.store.read()["observations"][created["id"]]
    assert result["validation"]["decision"] == decision
    assert result["causal_claim"] is False
    if paired:
        assert len(result["comparisons"]) == 4
        assert result["control_summary"]["rounds"] == result["summary"]["rounds"] == 16
        assert result["summary"]["cost_per_success"] == 0.1
        assert result["control_summary"]["cost_per_success"] == 0.2
    with pytest.raises(ValueError, match="new tasks"):
        observe(service, operation["id"], plan, key="repeated-data", compare_previous=paired)


def test_paired_observation_cannot_relax_independent_thresholds(adopted):
    service, operation, plan = adopted
    altered = plan.model_copy(update={"minimum_gain": 0})
    with pytest.raises(ValueError, match="thresholds"):
        observe(service, operation["id"], altered, key="relaxed", compare_previous=True)
