"""Version and decision invariants at the shared application service boundary."""

import pytest

from evals_app.fixtures import fixture_plan
from evals_app.models import Proposal, fingerprint, now
from evals_app.service import Service
from evals_app.store import Conflict


@pytest.fixture
def prepared(tmp_path):
    service = Service(tmp_path, background=False)
    plan = fixture_plan()
    service.register(plan.application, key="app")
    created = service.create(
        plan,
        [
            Proposal(
                changes={"reuse": True},
                source="application_rsi",
                hypothesis="reuse",
                risk="stale evidence",
                expected_baseline=plan.application.version,
            )
        ],
        key="experiment",
        use_gepa=False,
    )
    experiment_id = created["id"]
    candidate_id = next(
        k
        for k in service.store.read()["experiments"][experiment_id]["candidates"]
        if k != "baseline"
    )
    yield service, plan, experiment_id, candidate_id
    service.close()


def mark_verified(service, experiment_id, candidate_id, decision="recommend"):
    # Receipt tests isolate version transitions; real verification has separate runtime coverage.
    service.runtime.patch(
        experiment_id,
        status="completed",
        reports={candidate_id: {"decision": decision, "baseline": "fixture-v1"}},
    )


def test_unverified_and_insufficient_candidates_cannot_be_adopted(prepared):
    service, plan, experiment_id, candidate_id = prepared
    for decision in (None, "reject", "insufficient_evidence"):
        if decision:
            mark_verified(service, experiment_id, candidate_id, decision)
        with pytest.raises(ValueError, match="independently recommended"):
            service.operation(
                experiment_id, candidate_id, plan.application.version, "v2", key=str(decision)
            )


def test_receipt_identity_idempotency_and_actual_version(prepared):
    service, plan, experiment_id, candidate_id = prepared
    mark_verified(service, experiment_id, candidate_id)
    operation = service.operation(
        experiment_id, candidate_id, plan.application.version, "v2", key="adopt"
    )
    assert (
        service.operation(experiment_id, candidate_id, plan.application.version, "v2", key="adopt")
        == operation
    )
    kwargs = {
        "executor": "local",
        "actual_version": "v2",
        "config_fingerprint": operation["config_fingerprint"],
        "status": "applied",
        "evidence": {
            "source": "isolated verifier",
            "locator": "effective-config",
            "collected_at": now(),
        },
        "key": "receipt",
    }
    with pytest.raises(PermissionError):
        service.receipt(operation["id"], **{**kwargs, "executor": "forged"})
    with pytest.raises(Conflict):
        service.receipt(operation["id"], **{**kwargs, "actual_version": "wrong"})
    assert service.store.read()["applications"][plan.application.id]["version"] == "fixture-v1"
    receipt = service.receipt(operation["id"], **kwargs)
    assert service.receipt(operation["id"], **kwargs) == receipt
    with pytest.raises(Conflict):
        service.receipt(operation["id"], **{**kwargs, "key": "out-of-order"})
    with pytest.raises(Conflict, match="revalidated"):
        service.operation(experiment_id, candidate_id, "v2", "v3", key="stale-candidate")
    rollback = service.operation(
        experiment_id, candidate_id, "v2", "fixture-v1", key="rollback", rollback=operation["id"]
    )
    assert rollback["config"] == plan.application.config
    service.receipt(
        rollback["id"],
        **{
            **kwargs,
            "actual_version": "fixture-v1",
            "config_fingerprint": fingerprint(plan.application.config),
            "key": "rollback-receipt",
        },
    )
    assert service.store.read()["applications"][plan.application.id]["version"] == "fixture-v1"


def test_partial_failure_stops_new_work(prepared):
    service, plan, experiment_id, candidate_id = prepared
    mark_verified(service, experiment_id, candidate_id)
    operation = service.operation(experiment_id, candidate_id, "fixture-v1", "v2", key="adopt")
    service.receipt(
        operation["id"],
        executor="local",
        actual_version="partial-v2",
        config_fingerprint="partial",
        status="partial_failure",
        key="partial",
        evidence={"source": "state check", "locator": "partial-state", "collected_at": now()},
    )
    with pytest.raises(Conflict, match="reconciliation"):
        service.create(plan, [], key="must-not-run")


def test_restart_preserves_cancel_intent_and_reconciles_in_flight(prepared):
    service, plan, experiment_id, _ = prepared
    service.runtime.patch(experiment_id, status="running", stage="selection")
    reservation = service.runtime.reserve(experiment_id, "application", 0.1)
    service.runtime.patch(experiment_id, control="cancel", status="cancel_pending")
    service._recover()
    experiment = service.store.read()["experiments"][experiment_id]
    assert experiment["status"] == "interrupted" and experiment["control"] == "cancel"
    assert experiment["ledger"]["application"]["unknown"] == 1
    with pytest.raises(Conflict):
        service.recover(
            experiment_id,
            "changed-plan",
            {},
            {"source": "check", "locator": "state", "collected_at": now()},
        )
    result = service.recover(
        experiment_id,
        experiment["fingerprint"],
        {reservation: 0.12},
        {
            "source": "remote state verification",
            "locator": "recovery-report",
            "collected_at": now(),
        },
    )
    assert result["status"] == "cancelled"
    assert (
        service.store.read()["experiments"][experiment_id]["ledger"]["application"]["spent"] == 0.12
    )
