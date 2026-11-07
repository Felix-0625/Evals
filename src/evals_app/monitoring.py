"""Post-adoption observations and predeclared, equal-budget strategy studies."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from evals_app.logs import load_trials
from evals_app.models import Case, Plan, Status, fingerprint, now
from evals_app.store import Conflict
from evals_app.strategy_studies import (
    evaluate_study as evaluate_study,
)
from evals_app.strategy_studies import (
    freeze_study as freeze_study,
)
from evals_app.strategy_studies import (
    promote_strategy as promote_strategy,
)
from evals_app.verification import compare, summary

if TYPE_CHECKING:
    from evals_app.service import Service


def observation_binding(
    snapshot: dict[str, Any], operation_id: str | None, plan: Plan, compare_previous: bool
) -> dict[str, Any]:
    if operation_id is None:
        if compare_previous:
            raise ValueError("A configuration control requires an adoption operation")
        return {}
    operation = snapshot["operations"][operation_id]
    if operation["status"] != "applied":
        raise Conflict("Post-adoption observation requires an effective-version receipt")
    if (
        plan.application.id != operation["application_id"]
        or plan.application.version != operation["actual_version"]
        or plan.application.config != operation["config"]
    ):
        raise Conflict("Observation must use the actually adopted application and configuration")
    original = snapshot["experiments"][operation["experiment_id"]]
    if plan.dataset_version == original["plan"]["dataset_version"]:
        raise ValueError("Post-adoption tasks require a new dataset version")
    old_cases = original["plan"]["cases"] + [
        case
        for experiment in snapshot["experiments"].values()
        if experiment.get("observation_of") and experiment["application_id"] == plan.application.id
        for case in experiment["plan"]["cases"]
    ]
    if {c.group for c in plan.cases} & {c["group"] for c in old_cases} or {
        fingerprint(c.input) for c in plan.cases
    } & {fingerprint(c["input"]) for c in old_cases}:
        raise ValueError("Post-adoption observation requires new tasks and sessions")
    if compare_previous:
        fields = (
            "objective",
            "minimum_gain",
            "quality_tolerance",
            "minimum_coverage",
            "minimum_groups",
            "confidence",
            "bootstrap_samples",
            "hard_dimensions",
            "scorer_version",
        )
        if any(getattr(plan, field) != original["plan"][field] for field in fields):
            raise ValueError(
                "Post-adoption control must retain the independent validation thresholds"
            )
        rules = {fingerprint(r) for case in plan.cases for r in case.rules}
        old_rules = {fingerprint(r) for case in original["plan"]["cases"] for r in case["rules"]}
        if rules != old_rules:
            raise ValueError("Post-adoption control must retain the calibrated business rules")
        plan.application.validate_changes(operation["previous_config"])
    return {
        "observation_of": operation_id,
        "observation_mode": "paired_config" if compare_previous else "descriptive",
        "observation_control": {
            "config": operation["previous_config"],
            "original_version": operation["expected_baseline"],
            "execution_version": operation["actual_version"],
        }
        if compare_previous
        else None,
    }


def observe(
    service: "Service", operation_id: str, plan: Plan, *, key: str, compare_previous: bool = False
) -> Any:
    created = service.create(
        plan,
        [],
        key=f"observation:{key}",
        use_gepa=False,
        observation_of=operation_id,
        compare_previous=compare_previous,
    )
    experiment_id = created["id"]
    experiment = service.store.read()["experiments"][experiment_id]
    if experiment["status"] == "draft":
        service.start(experiment_id, created["fingerprint"])
    return created


def run_observation(service: "Service", experiment_id: str, plan: Plan) -> None:
    experiment = service.store.read()["experiments"][experiment_id]
    paired = experiment.get("observation_mode") == "paired_config"
    partitions = (
        ("train", "selection", "holdout", "counterexample") if paired else ("train", "selection")
    )
    comparisons = []
    for index, split in enumerate(partitions):
        service._phase(
            experiment_id, "post_adoption_control" if paired else "post_adoption_observation"
        )
        if paired:
            order = (
                ("previous_config", "baseline") if index % 2 else ("baseline", "previous_config")
            )
            rows = {
                name: service._evaluate(experiment_id, plan, experiment["candidates"][name], split)
                for name in order
            }
            comparisons.append(
                compare(plan, rows["previous_config"], rows["baseline"], phase=split)
            )
        else:
            service._evaluate(experiment_id, plan, experiment["candidates"]["baseline"], split)
    service.runtime.checkpoint(experiment_id)
    service.runtime.patch(
        experiment_id,
        status="completed",
        stage="finished",
        reason=None,
        conclusion="post_adoption_observation",
        observation_comparisons=comparisons,
    )
    record_observation(service, experiment_id)


def record_observation(service: "Service", experiment_id: str) -> None:
    snapshot = service.store.read()
    experiment = snapshot["experiments"][experiment_id]
    operation_id = experiment.get("observation_of")
    if not operation_id:
        return
    operation = snapshot["operations"][operation_id]
    trials = [
        trial
        for key, run in experiment["runs"].items()
        if key.startswith("baseline:")
        for trial in load_trials(run)
    ]
    control = [
        trial
        for key, run in experiment["runs"].items()
        if key.startswith("previous_config:")
        for trial in load_trials(run)
    ]
    hard_failures = [
        j
        for t in trials
        for j in t.judgments
        if j.dimension in experiment["plan"]["hard_dimensions"] and j.status == Status.FAIL
    ]
    comparisons = experiment.get("observation_comparisons", [])
    decision = (
        "reject"
        if hard_failures or any(r["decision"] == "reject" for r in comparisons)
        else "recommend"
        if len(comparisons) == 4 and all(r["decision"] == "recommend" for r in comparisons)
        else "insufficient_evidence"
    )
    record = {
        "id": experiment_id,
        "operation_id": operation_id,
        "application_id": experiment["application_id"],
        "version": operation["actual_version"],
        "provenance": experiment["plan"]["application"]["provenance"],
        "started_at": experiment["created_at"],
        "completed_at": now(),
        "summary": summary(trials),
        "hard_failures": len(hard_failures),
        "control_summary": summary(control) if control else None,
        "comparisons": comparisons,
        "validation": {
            "decision": decision,
            "mode": experiment.get("observation_mode", "descriptive"),
        },
        "rollback_basis": "Hard constraint or paired configuration regression on new tasks"
        if decision == "reject"
        else None,
        "causal_claim": False,
        "comparison_basis": "Paired configurations on new tasks within the effective code version"
        if control
        else "New-traffic description; distribution may differ",
        "control": experiment.get("observation_control"),
    }
    service.store.update(lambda state: state["observations"].update({experiment_id: record}))


def experience(service: "Service", content: dict[str, Any], *, key: str) -> Any:
    required = {"id", "version", "text", "application_id", "source", "expires_at", "enabled"}
    if (
        set(content) != required
        or not all(
            isinstance(content.get(name), str) and content[name].strip()
            for name in required - {"enabled"}
        )
        or type(content.get("enabled")) is not bool
    ):
        raise ValueError("Experience requires version, scope, source and expiry")
    expires = datetime.fromisoformat(content["expires_at"])
    if expires.tzinfo is None or expires <= datetime.now(UTC):
        raise ValueError("Experience has expired")

    def update(state: dict[str, Any]) -> Any:
        if content["application_id"] not in state["applications"]:
            raise KeyError(content["application_id"])
        name = f"{content['id']}:{content['version']}"
        if name in state["experiences"]:
            raise Conflict("Create a new experience version instead of overwriting it")
        state["experiences"][name] = content
        return content

    return service.store.update(update, key=f"experience:{key}", payload=content)


def disable_experience(service: "Service", experience_id: str, version: str) -> Any:
    def update(state: dict[str, Any]) -> Any:
        item = state["experiences"][f"{experience_id}:{version}"]
        item["enabled"] = False
        item["disabled_at"] = now()
        return item

    return service.store.update(update)


def active_experiences(service: "Service", application_id: str) -> list[dict[str, Any]]:
    if application_id not in service.store.read()["applications"]:
        raise KeyError(application_id)
    return [
        item
        for item in service.store.read()["experiences"].values()
        if item["application_id"] == application_id
        and item["enabled"]
        and datetime.fromisoformat(item["expires_at"]) > datetime.now(UTC)
    ]


def regression_case(
    service: "Service",
    experiment_id: str,
    sample_id: str,
    case: Case,
    *,
    redaction_note: str,
    key: str,
) -> Any:
    if not redaction_note.strip() or case.split != "counterexample":
        raise ValueError("Case reuse requires an explicit redaction note and regression partition")
    snapshot = service.store.read()
    experiment = snapshot["experiments"][experiment_id]
    if (
        experiment["status"] != "completed"
        or experiment["plan"]["application"]["provenance"] != "real"
    ):
        raise Conflict("Case reuse requires a completed real-application experiment")
    failed = [
        trial
        for run in experiment["runs"].values()
        for trial in (t.model_dump(mode="json") for t in load_trials(run))
        if trial["sample_id"] == sample_id
        and any(j["status"] == "fail" for j in trial["judgments"])
    ]
    if not failed:
        raise ValueError("Source case has no verified failure")
    content_hash = fingerprint(case.input)

    def update(state: dict[str, Any]) -> Any:
        pool = state.setdefault("regression_cases", {})
        if content_hash in pool:
            raise Conflict("Regression input is already registered")
        record = {
            "id": content_hash,
            "application_id": experiment["application_id"],
            "case": case.model_dump(mode="json"),
            "source_experiment": experiment_id,
            "source_sample": sample_id,
            "source_logs": sorted({t["log"] for t in failed}),
            "redaction_note": redaction_note,
            "approved_by": "local",
            "created_at": now(),
            "purpose": "regression; new terminal holdout required for later tuning",
        }
        pool[content_hash] = record
        return record

    return service.store.update(
        update,
        key=f"regression:{key}",
        payload={
            "case": case.model_dump(mode="json"),
            "source_experiment": experiment_id,
            "source_sample": sample_id,
            "redaction_note": redaction_note,
        },
    )


def regression_binding(state: dict[str, Any], plan: Plan) -> dict[str, Any]:
    """Keep reviewed regression content distinct from unseen terminal holdouts."""
    pool = state.get("regression_cases", {})
    used: set[str] = set()
    tuned_sources: set[str] = set()
    for case in plan.cases:
        reference = case.metadata.get("regression_case_id")
        matches = [
            item
            for item in pool.values()
            if item["application_id"] == plan.application.id and item["case"]["input"] == case.input
        ]
        if reference is not None:
            if not isinstance(reference, str) or reference not in pool:
                raise ValueError("Unknown approved regression case")
            item = pool[reference]
            if item["application_id"] != plan.application.id:
                raise ValueError("Regression case belongs to another application")
            matches = [item]
        for item in matches:
            reviewed = Case.model_validate(item["case"])
            metadata = {k: v for k, v in case.metadata.items() if k != "regression_case_id"}
            approved_metadata = {
                k: v for k, v in reviewed.metadata.items() if k != "regression_case_id"
            }
            if (
                case.input != reviewed.input
                or case.rules != reviewed.rules
                or metadata != approved_metadata
            ):
                raise ValueError("Changed regression content requires a new redaction review")
            if case.split == "holdout":
                raise ValueError("A reviewed regression case cannot be an unseen holdout")
            used.add(item["id"])
            if case.split in {"train", "selection"}:
                tuned_sources.add(item["source_experiment"])
    for source in tuned_sources:
        original = state["experiments"][source]["plan"]
        old_inputs = {case["input"] for case in original["cases"]}
        old_groups = {case["group"] for case in original["cases"]}
        if plan.dataset_version == original["dataset_version"] or any(
            case.split == "holdout" and (case.input in old_inputs or case.group in old_groups)
            for case in plan.cases
        ):
            raise ValueError("Tuning from failed cases requires a new independent terminal holdout")
    return {"regression_sources": sorted(used)} if used else {}
