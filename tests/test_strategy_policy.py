"""Controlled labels test study arithmetic and promotion, not real RSI performance."""

from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from evals_app.api import routes
from evals_app.fixtures import fixture_plan
from evals_app.models import Plan, fingerprint
from evals_app.monitoring import evaluate_study, freeze_study, promote_strategy
from evals_app.policies import BUILTIN_POLICY, SIMPLE_POLICY, SearchPolicy
from evals_app.runtime import BudgetExceeded
from evals_app.service import Service
from evals_app.store import Conflict


@pytest.fixture
def study_workspace(tmp_path):
    service = Service(tmp_path, background=False)
    plan = fixture_plan(groups=2)
    service.register(plan.application, key="app")
    policies = {
        "old": BUILTIN_POLICY,
        "simple_rule": SIMPLE_POLICY,
        "new": SearchPolicy(version="search-v2", configuration_candidates=True),
    }
    templates = {
        arm: service.create(plan, [], key=arm, policy=policy)["id"]
        for arm, policy in policies.items()
    }
    arms = {arm: [f"{arm}-{i}" for i in range(40)] for arm in policies}

    def seed(state):
        for index in range(40):
            raw = plan.model_dump(mode="json")
            for case in raw["cases"]:
                for field in ("id", "group", "input"):
                    case[field] = f"unit-{index}-" + case[field]
            frozen = Plan.model_validate(raw)
            for arm in policies:
                item = deepcopy(state["experiments"][templates[arm]])
                item.update(id=arms[arm][index], plan=raw, fingerprint=fingerprint(frozen))
                state["experiments"][item["id"]] = item

    service.store.update(seed)
    study = freeze_study(service, "search-v2", arms, 100, key="study")

    def measured(state):
        for arm, ids in arms.items():
            for experiment_id in ids:
                experiment = state["experiments"][experiment_id]
                experiment["status"] = "completed"
                if arm != "new":
                    continue
                candidate = next(k for k in experiment["candidates"] if k != "baseline")
                experiment["reports"] = {candidate: {"decision": "recommend"}}
                operation_id = f"op-{experiment_id}"
                state["operations"][operation_id] = {
                    "id": operation_id,
                    "experiment_id": experiment_id,
                    "candidate_id": candidate,
                    "status": "applied",
                    "rollback_of": None,
                }
                state["observations"][experiment_id] = {
                    "operation_id": operation_id,
                    "validation": {"decision": "recommend"},
                    "hard_failures": 0,
                    "summary": {"successes": 8},
                }

    service.store.update(measured)
    yield service, plan, study, templates
    service.close()


def test_only_sufficient_study_changes_defaults_and_existing_experiments_stay_frozen(
    study_workspace,
):
    service, plan, study, templates = study_workspace
    assert study["promotion_eligible"] is True
    report = evaluate_study(service, study["id"])
    assert report["decision"] == "recommend"
    assert report["results"]["new"]["false_rate_upper_bound"] < 0.1
    result = promote_strategy(service, study["id"], BUILTIN_POLICY.version, key="promote")
    assert promote_strategy(service, study["id"], BUILTIN_POLICY.version, key="promote") == result
    created = service.create(plan, [], key="after-promotion")
    snapshot = service.store.read()
    assert snapshot["experiments"][created["id"]]["policy"]["version"] == "search-v2"
    assert len(snapshot["experiments"][created["id"]]["candidates"]) > 1
    assert snapshot["experiments"][templates["old"]]["policy"] == BUILTIN_POLICY.model_dump(
        mode="json"
    )
    other = fixture_plan("harness", groups=2)
    service.register(other.application, key="another-application")
    unrelated = service.create(other, [], key="unrelated")
    assert service.store.read()["experiments"][unrelated["id"]][
        "policy"
    ] == BUILTIN_POLICY.model_dump(mode="json")
    with pytest.raises(Conflict, match="Default strategy changed"):
        promote_strategy(service, study["id"], BUILTIN_POLICY.version, key="stale")


@pytest.mark.parametrize("mode", ["descriptive", "later_regression", "insufficient"])
def test_promotion_rechecks_later_evidence_and_keeps_default_on_failure(study_workspace, mode):
    service, _, study, _ = study_workspace
    # A previously calculated recommendation must not hide subsequent failures.
    assert evaluate_study(service, study["id"])["decision"] == "recommend"

    def update(state):
        for observation in state["observations"].values():
            observation["validation"] = {
                "decision": "reject" if mode == "later_regression" else "insufficient_evidence"
            }
            if mode == "descriptive":
                observation.pop("validation")

    service.store.update(update)
    with pytest.raises(ValueError, match="sufficient evidence"):
        promote_strategy(service, study["id"], BUILTIN_POLICY.version, key=mode)
    assert "default_strategy" not in service.store.read()


def test_policy_versions_cannot_change_their_meaning(study_workspace):
    service, plan, _, _ = study_workspace
    with pytest.raises(Conflict, match="immutable"):
        service.create(
            plan, [], key="rewrite", policy=SearchPolicy(version="search-v2", use_gepa=False)
        )


def test_http_promotion_and_default_selection_share_the_service_gate(study_workspace):
    service, plan, study, _ = study_workspace
    app = FastAPI()
    app.include_router(routes(service), prefix="/api")
    with TestClient(app) as client:
        body = {"expected_default_version": BUILTIN_POLICY.version}
        url = f"/api/evals/strategies/{study['id']}/promote"
        result = client.post(url, json=body, headers={"Idempotency-Key": "promote-http"})
        assert result.status_code == 200, result.text
        assert result.json()["application_ids"] == [plan.application.id]
        assert (
            client.post(url, json=body, headers={"Idempotency-Key": "promote-http"}).json()
            == result.json()
        )
        created = client.post(
            "/api/evals/experiments",
            json={"plan": plan.model_dump(mode="json")},
            headers={"Idempotency-Key": "using-default"},
        )
        assert created.status_code == 200, created.text
        saved = client.get(f"/api/evals/experiments/{created.json()['id']}?view=summary").json()
        assert saved["policy"]["version"] == "search-v2"
        assert len(saved["candidates"]) > 1


def test_strategy_budget_reserves_across_units_and_cost_categories(tmp_path):
    service = Service(tmp_path, background=False)
    try:
        original = fixture_plan(groups=2)
        service.register(original.application, key="app")
        policies = {
            "old": BUILTIN_POLICY,
            "simple_rule": SIMPLE_POLICY,
            "new": SearchPolicy(version="search-v2", configuration_candidates=True),
        }
        arms = {arm: [] for arm in policies}
        for unit in range(2):
            raw = original.model_dump(mode="json")
            raw["budget"]["analysis"] = 10
            for case in raw["cases"]:
                for field in ("id", "group", "input"):
                    case[field] = f"unit-{unit}-{case[field]}"
            plan = Plan.model_validate(raw)
            for arm, policy in policies.items():
                record = service.create(plan, [], key=f"{arm}-{unit}", policy=policy)
                arms[arm].append(record["id"])
        freeze_study(service, "search-v2", arms, 1, key="study")
        for experiment_id in (*arms["old"], arms["new"][0]):
            fields = service.store.experiment_fields(experiment_id, "fingerprint")
            service.start(experiment_id, fields["fingerprint"])
        old_first, old_second = arms["old"]
        new_first = arms["new"][0]
        runtime = service.runtime
        pending = runtime.reserve(old_first, "application", 0.75)
        with pytest.raises(BudgetExceeded, match="Strategy arm budget"):
            runtime.reserve(old_second, "analysis", 0.30)
        other = runtime.reserve(new_first, "analysis", 0.30)
        runtime.settle(new_first, other, 0.30)
        runtime.settle(old_first, pending, 0.60)
        second = runtime.reserve(old_second, "analysis", 0.30)
        runtime.settle(old_second, second, 0.30)
        with pytest.raises(BudgetExceeded, match="Strategy arm budget"):
            runtime.reserve(old_first, "application", 0.20)
        trials = service._evaluate(
            old_second, plan, {"id": "baseline", "config": plan.application.config}, "train"
        )
        assert len(trials) == 4
        assert all(not trial.attempts and trial.observation is None for trial in trials)
        assert (
            service.store.experiment_fields(old_second, "ledger")["ledger"]["application"]["spent"]
            == 0
        )
        uncertain = runtime.reserve(old_first, "application", 0.05)
        runtime.settle(old_first, uncertain, None)
        with pytest.raises(BudgetExceeded, match="Strategy arm.*unknown"):
            runtime.reserve(old_second, "analysis", 0)
    finally:
        service.close()


def test_a_draft_experiment_cannot_join_two_strategy_studies(tmp_path):
    service = Service(tmp_path, background=False)
    try:
        plan = fixture_plan(groups=2)
        service.register(plan.application, key="app")
        arms = {
            arm: [service.create(plan, [], key=arm, policy=policy)["id"]]
            for arm, policy in {
                "old": BUILTIN_POLICY,
                "simple_rule": SIMPLE_POLICY,
                "new": SearchPolicy(version="search-v2", configuration_candidates=True),
            }.items()
        }
        frozen = freeze_study(service, "search-v2", arms, 1, key="first")
        assert freeze_study(service, "search-v2", arms, 1, key="first") == frozen
        with pytest.raises(Conflict, match="already belongs"):
            freeze_study(service, "search-v2", arms, 2, key="second")
    finally:
        service.close()
