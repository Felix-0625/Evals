import json
from types import SimpleNamespace

import anyio
import pytest
from gepa.core.adapter import EvaluationBatch

from evals_app.evolution import InspectAdapter
from evals_app.fixtures import fixture_plan, synthetic_application
from evals_app.models import Observation, Status, Trial, fingerprint
from evals_app.scoring import judge
from evals_app.verification import InsufficientEvidence


def adapter():
    # Runtime is unused by feedback construction and per-trial score validation.
    return InspectAdapter(None, "experiment", fixture_plan())


def batch():
    return EvaluationBatch(
        outputs=[None],
        scores=[0.0],
        trajectories=[
            {
                "sample_id": "development-1",
                "trials": [
                    {
                        "judgments": [
                            {
                                "dimension": "result",
                                "status": "pass",
                                "reason": "SECRET successful state",
                                "evidence": [{"scope": "shareable"}],
                            }
                        ],
                        "observation": {
                            "facts": {"result": {"success": True}, "private": {"value": "SECRET"}},
                            "evidence": {
                                "result.success": {
                                    "scope": "shareable",
                                    "locator": "approved#state",
                                },
                                "private.value": {"scope": "local", "locator": "private#value"},
                            },
                        },
                    }
                ],
            }
        ],
    )


def test_application_scope_overrides_individual_shareable_references():
    item = adapter()
    feedback = item.make_reflective_dataset({}, batch(), ["prompt"])
    assert "SECRET" not in json.dumps(feedback)
    assert "approved#state" not in json.dumps(feedback)


def test_shareable_feedback_includes_only_approved_raw_evidence():
    item = adapter()
    item.plan = item.plan.model_copy(
        update={
            "application": item.plan.application.model_copy(update={"evidence_scope": "shareable"})
        }
    )
    evaluation = batch()
    evaluation.trajectories[0]["trials"][0]["judgments"][0]["reason"] = "approved business state"
    feedback = item.make_reflective_dataset({}, evaluation, ["prompt"])
    assert "approved#state" in json.dumps(feedback)
    assert "SECRET" not in json.dumps(feedback)


def test_reflection_withholds_composite_evidence_with_local_children():
    item = adapter()
    item.plan = item.plan.model_copy(
        update={
            "application": item.plan.application.model_copy(update={"evidence_scope": "shareable"})
        }
    )
    evaluation = batch()
    trial = evaluation.trajectories[0]["trials"][0]
    reference = {"scope": "shareable", "locator": "composite-reference"}
    trial["observation"]["facts"]["result"]["secret"] = "PRIVATE-COMPOSITE-CHILD"
    trial["observation"]["evidence"].update(
        {
            "result": reference,
            "result.secret": {"scope": "local", "locator": "private-reference"},
        }
    )
    trial["judgments"][0].update(
        reason="PRIVATE-COMPOSITE-CHILD in locally graded state", evidence=[reference]
    )
    exported = json.dumps(item.make_reflective_dataset({}, evaluation, ["prompt"]))
    assert "PRIVATE-COMPOSITE-CHILD" not in exported
    assert "composite-reference" not in exported
    assert "approved#state" in exported


def test_missing_scoring_is_not_an_optimizer_failure_score():
    from types import SimpleNamespace

    trial = SimpleNamespace(
        judgments=[SimpleNamespace(critical=False, status=Status.GRADING_ERROR, dimension="result")]
    )
    with pytest.raises(InsufficientEvidence, match="grading failure"):
        adapter().search_score(trial)


def test_optimizer_cannot_request_holdout_cases():
    from types import SimpleNamespace

    runtime = SimpleNamespace(checkpoint=lambda _: None)
    plan = fixture_plan()
    item = InspectAdapter(runtime, "experiment", plan)
    with pytest.raises(ValueError, match="development partitions"):
        item.evaluate([next(c for c in plan.cases if c.split == "holdout")], {"prompt": "grounded"})


def execution_rows(plan, cases, candidate, config):
    rows = []
    for case in cases:
        observation = Observation.model_validate(
            anyio.run(
                synthetic_application,
                {
                    "config": config,
                    "metadata": case.metadata,
                    "application_version": plan.application.version,
                },
            )
        )
        for epoch in range(1, plan.epochs + 1):
            rows.append(
                Trial(
                    sample_id=case.id,
                    group=case.group,
                    epoch=epoch,
                    attempt_id=f"{case.id}-{epoch}",
                    candidate_id=candidate,
                    run_id="owned-run",
                    config_fingerprint=fingerprint(config),
                    application_version=plan.application.version,
                    observation=observation,
                    judgments=judge(case, plan.application, observation),
                    attempts=[{"cost": observation.cost, "latency": 0.1}],
                    log="owned.eval",
                )
            )
    return rows


def test_optimizer_restores_batch_order_without_merging_epochs():
    plan = fixture_plan(groups=2)
    cases = [c for c in plan.cases if c.split == "train"]
    runtime = SimpleNamespace(
        checkpoint=lambda _: None,
        evaluate=lambda exp, p, candidate, config, batch: list(
            reversed(execution_rows(p, batch, candidate, config))
        ),
    )
    result = InspectAdapter(runtime, "exp", plan).evaluate(
        list(reversed(cases)), {"prompt": "grounded"}, True
    )
    assert [r["sample_id"] for r in result.trajectories] == [c.id for c in reversed(cases)]
    assert all([t["epoch"] for t in r["trials"]] == [1, 2] for r in result.trajectories)
    assert len(result.scores) == len(result.outputs) == len(cases)


@pytest.mark.parametrize(
    "change",
    [
        {"epoch": 2},
        {"sample_id": "foreign"},
        {"candidate_id": "foreign"},
        {"config_fingerprint": "foreign"},
        {"application_version": "foreign"},
        {"group": "foreign"},
        {"run_id": "foreign"},
        {"log": "foreign.eval"},
        {"judgments": []},
    ],
)
def test_optimizer_rejects_mixed_or_duplicate_execution_results(change):
    plan = fixture_plan(groups=2)
    cases = [c for c in plan.cases if c.split == "train"]

    def evaluate(exp, frozen, candidate, config, batch):
        rows = execution_rows(frozen, batch, candidate, config)
        rows[0] = rows[0].model_copy(update=change)
        return rows

    runtime = SimpleNamespace(checkpoint=lambda _: None, evaluate=evaluate)
    with pytest.raises(InsufficientEvidence):
        InspectAdapter(runtime, "exp", plan).evaluate(cases, {"prompt": "grounded"})
