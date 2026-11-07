import anyio
import pytest

from evals_app.fixtures import fixture_plan, synthetic_application
from evals_app.models import Dimension, Observation, Status, Trial, fingerprint
from evals_app.scoring import judge
from evals_app.verification import compare, summary


def trials(plan, config, *, missing=None):
    rows = []
    for case in plan.cases:
        if case.split != "holdout":
            continue
        for epoch in range(1, plan.epochs + 1):
            payload = {
                "config": config,
                "metadata": case.metadata,
                "application_version": plan.application.version,
            }
            observation = Observation.model_validate(anyio.run(synthetic_application, payload))
            if missing:
                observation = observation.model_copy(
                    update={
                        "evidence": {k: v for k, v in observation.evidence.items() if k != missing}
                    }
                )
            rows.append(
                Trial(
                    sample_id=case.id,
                    group=case.group,
                    epoch=epoch,
                    attempt_id=f"{case.id}-{epoch}",
                    candidate_id="baseline" if config == plan.application.config else "candidate",
                    run_id="controlled-run",
                    application_version=plan.application.version,
                    config_fingerprint=fingerprint(config),
                    observation=observation,
                    judgments=judge(case, plan.application, observation),
                    attempts=[{"cost": observation.cost, "latency": 0.1}],
                    log="controlled.eval",
                )
            )
    return rows


@pytest.mark.parametrize(
    "changes,missing,decision",
    [
        ({"reuse": True}, None, "recommend"),
        ({"prompt": "grounded"}, None, "reject"),
        ({"reuse": True, "policy": "unsafe"}, None, "reject"),
        ({"reuse": True}, "security.authorized", "insufficient_evidence"),
    ],
)
def test_independent_decisions(changes, missing, decision):
    plan = fixture_plan()
    original = trials(plan, plan.application.config)
    candidate = trials(plan, {**plan.application.config, **changes}, missing=missing)
    report = compare(plan, original, candidate)
    assert report["decision"] == decision
    if decision == "recommend":
        assert report["gain_interval"] == [0.1, 0.1]
        assert report["quality_interval"] == [0, 0]
        assert report["candidate"]["rounds"] == 16
        assert report["candidate"]["groups"] == 8


def test_duplicate_rounds_and_unknown_cost_cannot_recommend():
    plan = fixture_plan()
    original = trials(plan, plan.application.config)
    candidate = trials(plan, {**plan.application.config, "reuse": True})
    assert compare(plan, original, candidate + candidate[:1])["decision"] == "insufficient_evidence"
    unknown = [
        t.model_copy(update={"attempts": [{"cost": None, "latency": 0.1}]}) for t in candidate
    ]
    assert compare(plan, original, unknown)["decision"] == "insufficient_evidence"
    assert summary(unknown)["cost_per_success"] is None


def test_missing_epoch_retains_denominator_and_never_counts_as_pass():
    plan = fixture_plan()
    original = trials(plan, plan.application.config)
    candidate = trials(plan, {**plan.application.config, "reuse": True})
    candidate[0] = candidate[0].model_copy(
        update={
            "judgments": [
                j.model_copy(update={"status": Status.UNVERIFIABLE})
                if j.dimension == Dimension.RESULT
                else j
                for j in candidate[0].judgments
            ]
        }
    )
    coverage = summary(candidate)["dimensions"]["result"]
    assert coverage["applicable"] == 16
    assert coverage["confirmed_pass_ratio"] == 15 / 16
    assert coverage["valid_pass_rate"] == 1
    assert compare(plan, original, candidate)["decision"] == "insufficient_evidence"


def test_repeated_results_show_variation_and_group_coverage_without_dropping_missing():
    plan = fixture_plan()
    rows = trials(plan, plan.application.config)
    for index, status in ((0, Status.FAIL), (2, Status.UNVERIFIABLE)):
        rows[index] = rows[index].model_copy(
            update={
                "judgments": [
                    j.model_copy(update={"status": status})
                    if j.dimension == Dimension.RESULT
                    else j
                    for j in rows[index].judgments
                ]
            }
        )
    report = summary(rows)
    assert report["independent_samples"] == report["groups"] == 8
    assert report["rounds"] == 16
    assert report["mixed_outcome_samples"] == [rows[0].sample_id]
    assert report["missing_result_samples"] == [rows[2].sample_id]
    group = report["by_group"][rows[2].group]["dimensions"]["result"]
    assert group["applicable"] == 2
    assert group["coverage"] == group["confirmed_pass_ratio"] == 0.5
