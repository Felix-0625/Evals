import json
from pathlib import Path

import anyio
import pytest
from filelock import Timeout
from pydantic import ValidationError

from evals_app.calibration import calibrate, calibrate_business
from evals_app.fixtures import fixture_plan, synthetic_application
from evals_app.models import Plan, Proposal
from evals_app.service import Service
from evals_app.store import Conflict, Store


def test_calibration():
    result = calibrate()
    assert result["correct"] == result["expected"] == 8


def test_split_leakage_rejected():
    raw = fixture_plan().model_dump(mode="json")
    raw["cases"][8]["group"] = raw["cases"][0]["group"]
    with pytest.raises(ValidationError, match="cross data partitions"):
        Plan.model_validate(raw)


def test_store_lock_atomicity_and_idempotency(tmp_path: Path):
    store = Store(tmp_path)
    with pytest.raises(Timeout):
        Store(tmp_path)
    store.update(lambda s: s["applications"].update(a={"version": "1"}), key="one", payload=1)
    revision = store.read()["revision"]
    store.update(lambda s: pytest.fail("Repeated request executed"), key="one", payload=1)
    assert store.read()["revision"] == revision
    with pytest.raises(Conflict):
        store.update(lambda s: None, key="one", payload=2)
    with pytest.raises(Conflict):
        store.update(lambda s: None, revision=0)
    with pytest.raises(ValueError):
        store.update(lambda s: s.update(bad=float("nan")))
    assert "bad" not in store.read()
    store.close()
    restored = Store(tmp_path)
    assert restored.read()["applications"]["a"]["version"] == "1"
    restored.close()
    assert json.loads((tmp_path / "state.json").read_text())["revision"] == revision


def test_proposal_fingerprint_and_cancelled_experiment(tmp_path: Path):
    service = Service(tmp_path, background=False)
    plan = fixture_plan()
    service.register(plan.application, key="app")
    with pytest.raises(ValueError, match="Undeclared"):
        service.create(
            plan,
            [
                Proposal(
                    changes={"unknown": True},
                    source="application_rsi",
                    hypothesis="test",
                    risk="test",
                    expected_baseline="fixture-v1",
                )
            ],
            key="bad",
        )
    created = service.create(plan, [], key="good")
    with pytest.raises(Conflict):
        service.start(created["id"], "wrong")
    service.control(created["id"], "cancel")
    with pytest.raises(Conflict):
        service.start(created["id"], created["fingerprint"])
    service.close()


def test_calibration_does_not_authorize_a_different_business_rule(tmp_path: Path):
    raw = fixture_plan().model_dump(mode="json")
    raw["application"]["provenance"] = "real"
    plan = Plan.model_validate(raw)
    case = plan.cases[0]
    positive = anyio.run(
        synthetic_application,
        {
            "config": {**plan.application.config, "prompt": "grounded"},
            "metadata": case.metadata,
            "application_version": plan.application.version,
        },
    )
    negative = {**positive, "facts": {**positive["facts"], "result": {"success": False}}}
    annotations = [
        {
            "case": case.model_dump(mode="json"),
            "observation": value,
            "expected": {
                rule.dimension.value: (
                    "unverifiable"
                    if value is None
                    else "fail"
                    if label == "fail" and rule.dimension.value == "result"
                    else "pass"
                )
                for rule in case.rules
            },
        }
        for value, label in ((positive, "pass"), (negative, "fail"), (None, "unverifiable"))
    ]
    record = calibrate_business(plan.application, annotations, "rubric-v1", 1)
    raw["calibration"] = record["calibration"]
    service = Service(tmp_path, background=False)
    try:
        service.register(plan.application, key="app")
        service.store.update(
            lambda state: state["calibrations"].update({record["calibration"]["artifact"]: record})
        )
        service.create(Plan.model_validate(raw), [], key="matching")
        raw["cases"][0]["rules"][0]["expected"] = False
        with pytest.raises(ValueError, match="absent from calibration"):
            service.create(Plan.model_validate(raw), [], key="changed-rule")
    finally:
        service.close()
