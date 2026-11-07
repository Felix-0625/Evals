"""Protocol fixtures; real native-log replay is in acceptance_case_reuse.py."""

from copy import deepcopy

import pytest

from evals_app.fixtures import fixture_plan
from evals_app.models import Plan, fingerprint
from evals_app.monitoring import regression_binding, regression_case
from evals_app.service import Service
from evals_app.store import Conflict


@pytest.fixture
def reviewed(tmp_path, monkeypatch):
    plan = fixture_plan(groups=2)
    service = Service(tmp_path, background=False)
    service.register(plan.application, key="app")
    created = service.create(plan, [], key="source", use_gepa=False)
    source_id = created["id"]
    source_case = plan.cases[0]
    trial = {"sample_id": source_case.id, "judgments": [{"status": "fail"}], "log": "native.eval"}

    class RecordedTrial:
        def model_dump(self, **kwargs):
            return deepcopy(trial)

    monkeypatch.setattr("evals_app.monitoring.load_trials", lambda run: [RecordedTrial()])

    def finish(state):
        source = state["experiments"][source_id]
        source["status"] = "completed"
        source["plan"]["application"]["provenance"] = "real"
        source["runs"] = {"record": {"log": "native.eval"}}

    service.store.update(finish)
    case = source_case.model_copy(
        update={
            "id": "reviewed",
            "group": "reviewed",
            "split": "counterexample",
            "input": "redacted task",
        }
    )
    yield service, plan, source_id, case, trial
    service.close()


def test_regression_registration_requires_real_completed_failure_and_redaction(reviewed):
    service, _, source_id, case, trial = reviewed
    with pytest.raises(ValueError, match="redaction"):
        regression_case(
            service, source_id, trial["sample_id"], case, redaction_note=" ", key="blank"
        )
    with pytest.raises(ValueError, match="verified failure"):
        regression_case(
            service, source_id, "missing", case, redaction_note="reviewed", key="missing"
        )
    for status, provenance in (("draft", "real"), ("completed", "fixture")):

        def update(state, status=status, provenance=provenance):
            source = state["experiments"][source_id]
            source["status"] = status
            source["plan"]["application"]["provenance"] = provenance

        service.store.update(update)
        with pytest.raises(Conflict, match="completed real"):
            regression_case(
                service, source_id, trial["sample_id"], case, redaction_note="reviewed", key=status
            )


def register(reviewed):
    service, _, source_id, case, trial = reviewed
    return regression_case(
        service,
        source_id,
        trial["sample_id"],
        case,
        redaction_note="Reviewed generated data only",
        key="review",
    )


def test_case_pool_idempotency_duplicate_and_reviewed_content(reviewed):
    service, plan, source_id, case, trial = reviewed
    record = register(reviewed)
    assert register(reviewed) == record
    assert record["source_logs"] == ["native.eval"] and record["approved_by"] == "local"
    with pytest.raises(Conflict, match="already registered"):
        regression_case(
            service, source_id, trial["sample_id"], case, redaction_note="reviewed", key="duplicate"
        )
    changed = case.model_copy(
        update={
            "input": "unreviewed content",
            "metadata": {**case.metadata, "regression_case_id": record["id"]},
        }
    )
    raw = plan.model_dump(mode="json")
    raw["cases"][0] = changed.model_copy(update={"split": "train"}).model_dump(mode="json")
    with pytest.raises(ValueError, match="new redaction review"):
        service.create(Plan.model_validate(raw), [], key="unreviewed", use_gepa=False)


def test_reused_failure_requires_new_holdout_and_is_rechecked_at_start(reviewed):
    service, plan, _, case, _ = reviewed
    record = register(reviewed)
    raw = plan.model_dump(mode="json")
    raw["dataset_version"] = "next-v2"
    raw["cases"][0] = case.model_copy(update={"split": "train"}).model_dump(mode="json")
    with pytest.raises(ValueError, match="new independent terminal holdout"):
        service.create(Plan.model_validate(raw), [], key="old-holdout", use_gepa=False)
    for row in raw["cases"]:
        if row["split"] == "holdout":
            row.update(input=f"fresh {row['input']}", group=f"fresh {row['group']}")
    independent = Plan.model_validate(raw)
    created = service.create(independent, [], key="fresh", use_gepa=False)
    assert service.store.read()["experiments"][created["id"]]["regression_sources"] == [
        record["id"]
    ]
    service.start(created["id"], created["fingerprint"])
    service.control(created["id"], "cancel")
    # A prepared plan must also see reviews recorded after its creation.
    before = plan.model_dump(mode="json")
    before["cases"][4] = case.model_copy(update={"split": "holdout"}).model_dump(mode="json")
    holdout_plan = Plan.model_validate(before)
    state = service.store.read()
    with pytest.raises(ValueError, match="unseen holdout"):
        regression_binding(state, holdout_plan)
    state["experiments"]["prepared-before-review"] = {
        **state["experiments"][created["id"]],
        "status": "draft",
        "plan": holdout_plan.model_dump(mode="json"),
        "fingerprint": fingerprint(holdout_plan),
    }
    service.store.update(
        lambda current: current["experiments"].update(
            {"prepared-before-review": state["experiments"]["prepared-before-review"]}
        )
    )
    with pytest.raises(ValueError, match="unseen holdout"):
        service.start("prepared-before-review", fingerprint(holdout_plan))
