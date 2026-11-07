from datetime import UTC, datetime, timedelta

import pytest

from evals_app.fixtures import fixture_plan
from evals_app.models import Proposal
from evals_app.monitoring import (
    active_experiences,
    disable_experience,
    evaluate_study,
    experience,
    freeze_study,
    observe,
)
from evals_app.service import Service
from evals_app.store import Conflict


@pytest.fixture
def registered(tmp_path):
    service = Service(tmp_path, background=False)
    plan = fixture_plan()
    service.register(plan.application, key="app")
    yield service, plan
    service.close()


def test_strategy_freeze_and_insufficient_evidence_preserve_old_policy(registered):
    service, plan = registered
    experiments = [
        service.create(plan, [], key=f"strategy-{i}", use_gepa=i == 2)["id"] for i in range(3)
    ]
    arms = dict(zip(("old", "simple_rule", "new"), [[i] for i in experiments], strict=True))
    study = freeze_study(service, "search-policy-v2", arms, 100, key="study")
    with pytest.raises(Conflict, match="already frozen"):
        service.submit(
            experiments[0],
            Proposal(
                changes={"reuse": True},
                source="manual",
                hypothesis="late change",
                risk="selection bias",
                expected_baseline="fixture-v1",
            ),
            key="late",
        )
    result = evaluate_study(service, study["id"])
    assert result["decision"] == "insufficient_evidence"
    assert "Too few independent paired strategy units" in result["reasons"]
    assert result["results"]["new"]["false_recommendation_rate"] is None


def test_experience_expiry_scope_and_disable(registered):
    service, plan = registered
    item = {
        "id": "retrieval-rule",
        "version": "v1",
        "text": "Retrieve supporting sources",
        "application_id": plan.application.id,
        "source": "verified-case-1",
        "enabled": True,
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    experience(service, item, key="rule")
    assert active_experiences(service, plan.application.id) == [item]
    disable_experience(service, item["id"], item["version"])
    assert not active_experiences(service, plan.application.id)
    with pytest.raises(ValueError, match="expired"):
        experience(service, {**item, "expires_at": "2000-01-01T00:00:00+00:00"}, key="expired")
    with pytest.raises(KeyError):
        active_experiences(service, "other-application")


def test_post_adoption_observation_requires_effective_receipt(registered):
    service, plan = registered
    service.store.update(lambda s: s["operations"].update({"op": {"status": "pending"}}))
    with pytest.raises(Conflict, match="effective-version receipt"):
        observe(service, "op", plan, key="premature")


@pytest.mark.parametrize(
    "condition,expected",
    [
        ("unknown_cost", "insufficient_evidence"),
        ("missing_observation", "insufficient_evidence"),
        ("budget_exceeded", "insufficient_evidence"),
        ("no_gain", "keep_old"),
    ],
)
def test_strategy_decision_requires_complete_cost_and_later_evidence(
    registered, condition, expected
):
    service, _ = registered
    arms = {arm: [f"{arm}-{i}" for i in range(8)] for arm in ("old", "simple_rule", "new")}

    def update(state):
        state["strategies"]["study"] = {
            "id": "study",
            "arms": arms,
            "minimum_units": 8,
            "maximum_false_rate": 0.1,
            "budget_per_arm": 10,
        }
        for arm, ids in arms.items():
            for experiment_id in ids:
                state["experiments"][experiment_id] = {
                    "status": "completed",
                    "reports": {"candidate": {"decision": "recommend"}}
                    if condition == "missing_observation" and arm == "new"
                    else {},
                    "ledger": {
                        "application": {
                            "spent": 2 if condition == "budget_exceeded" else 1,
                            "unknown": 1 if condition == "unknown_cost" else 0,
                            "reserved": 0,
                        }
                    },
                }

    service.store.update(update)
    result = evaluate_study(service, "study")
    assert result["decision"] == expected
    assert result["reasons"] if expected == "insufficient_evidence" else not result["reasons"]
