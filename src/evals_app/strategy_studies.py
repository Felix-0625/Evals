"""Predeclared equal-budget strategy comparisons and scoped promotion."""

from typing import TYPE_CHECKING, Any

import numpy as np

from evals_app.models import fingerprint, identifier, now
from evals_app.policies import SIMPLE_POLICY, SearchPolicy, default_policy, resolve_policy
from evals_app.store import Conflict

if TYPE_CHECKING:
    from evals_app.service import Service


def freeze_study(
    service: "Service", version: str, arms: dict[str, list[str]], budget: float, *, key: str
) -> Any:
    """Freeze paired independent units, policy versions and equal per-arm budgets."""
    if set(arms) != {"old", "simple_rule", "new"} or not np.isfinite(budget) or budget <= 0:
        raise ValueError(
            "A strategy study requires old, simple_rule and new arms and a fixed budget"
        )
    if len({len(arm) for arm in arms.values()}) != 1:
        raise ValueError("Strategy arms require equal numbers of paired task units")
    flat = [e for experiments in arms.values() for e in experiments]
    if not flat or len(flat) != len(set(flat)) or any(not arm for arm in arms.values()):
        raise ValueError("Strategy arms require distinct, nonempty experiment lists")

    def update(state: dict[str, Any]) -> Any:
        for experiment_id in flat:
            experiment = state["experiments"][experiment_id]
            if experiment["status"] != "draft":
                raise Conflict("Strategy assignments must freeze before any experiment is executed")
            if experiment.get("strategy_study"):
                raise Conflict("Experiment already belongs to a strategy study")
        units = []
        seen_groups: set[str] = set()
        for index in range(len(arms["old"])):
            paired = [state["experiments"][arms[arm][index]] for arm in arms]
            if len({e["fingerprint"] for e in paired}) != 1:
                raise ValueError(
                    "Paired strategy units must freeze identical evaluation conditions"
                )
            groups = {c["group"] for c in paired[0]["plan"]["cases"]}
            if groups & seen_groups:
                raise ValueError("Different strategy units must contain independent task groups")
            seen_groups.update(groups)
            units.append(paired[0]["fingerprint"])
        study_id = identifier("study")
        policies = {}
        for arm, experiment_ids in arms.items():
            values = [
                state["experiments"][e].get("policy")
                or resolve_policy(state, None, state["experiments"][e]["use_gepa"]).model_dump(
                    mode="json"
                )
                for e in experiment_ids
            ]
            if len({fingerprint(value) for value in values}) != 1:
                raise ValueError("Each strategy arm must freeze one reproducible search policy")
            policies[arm] = values[0]
        application_ids = sorted({state["experiments"][e]["application_id"] for e in flat})
        defaults = {
            app_id: default_policy(state, app_id).model_dump(mode="json")
            for app_id in application_ids
        }
        old = defaults[application_ids[0]]
        new = policies["new"]
        promotion_eligible = (
            all(policies["old"] == baseline for baseline in defaults.values())
            and policies["simple_rule"] == SIMPLE_POLICY.model_dump(mode="json")
            and new["version"] == version
            and any(new[k] != old[k] for k in ("use_gepa", "configuration_candidates"))
        )
        record = {
            "id": study_id,
            "version": version,
            "arms": arms,
            "budget_per_arm": budget,
            "unit_fingerprints": units,
            "minimum_units": 8,
            "maximum_false_rate": 0.1,
            "status": "frozen",
            "created_at": now(),
            "decision": "insufficient_evidence",
            "policies": policies,
            "default_baseline": old,
            "default_baselines": defaults,
            "application_ids": application_ids,
            "promotion_eligible": promotion_eligible,
        }
        for experiment_id in flat:
            state["experiments"][experiment_id]["strategy_study"] = study_id
        state["strategies"][study_id] = record
        return record

    return service.store.update(
        update, key=f"study:{key}", payload={"version": version, "arms": arms, "budget": budget}
    )


def _study_report(snapshot: dict[str, Any], study_id: str) -> dict[str, Any]:
    study = snapshot["strategies"][study_id]
    results: dict[str, Any] = {}
    reasons = []
    if len(study["arms"]["old"]) < study["minimum_units"]:
        reasons.append("Too few independent paired strategy units")
    for arm, experiment_ids in study["arms"].items():
        effective, false, observed, cost = 0, 0, 0, 0.0
        intervention_count, intervention_coverage = 0, 0
        unit_improvements = []
        for experiment_id in experiment_ids:
            experiment = snapshot["experiments"][experiment_id]
            if experiment.get("strategy_study", study_id) != study_id:
                reasons.append(f"{arm}: experiment no longer belongs to this study")
            if "operator_interventions" in experiment:
                intervention_count += len(experiment["operator_interventions"])
                intervention_coverage += 1
            unit_improvements.append(
                sum(r["decision"] == "recommend" for r in experiment["reports"].values())
            )
            if experiment["status"] != "completed":
                reasons.append(f"{arm}: experiment has not completed")
            ledgers = experiment["ledger"].values()
            if any(v["unknown"] or v["reserved"] for v in ledgers):
                reasons.append(f"{arm}: incomplete cost accounting")
            cost += sum(v["spent"] for v in ledgers)
            for candidate_id, report in experiment["reports"].items():
                if report["decision"] != "recommend":
                    continue
                effective += 1
                operations = [
                    o
                    for o in snapshot["operations"].values()
                    if o["experiment_id"] == experiment_id
                    and o["candidate_id"] == candidate_id
                    and o["status"] == "applied"
                    and not o["rollback_of"]
                ]
                observations = [
                    o
                    for o in snapshot["observations"].values()
                    if any(op["id"] == o["operation_id"] for op in operations)
                ]
                if not observations:
                    reasons.append(f"{arm}: recommended version lacks later observations")
                    continue
                decisions = [o.get("validation", {}).get("decision") for o in observations]
                if "reject" in decisions:
                    observed += 1
                    false += 1
                elif all(decision == "recommend" for decision in decisions):
                    observed += 1
                else:
                    reasons.append(
                        f"{arm}: later observations do not confirm configuration benefit"
                    )
        if cost > study["budget_per_arm"] + 1e-9:
            reasons.append(f"{arm}: fixed budget exceeded")
        if study.get("promotion_eligible") and intervention_coverage != len(experiment_ids):
            reasons.append(f"{arm}: operator intervention history is incomplete")
        results[arm] = {
            "verified_improvements": effective,
            "observed_recommendations": observed,
            "later_failures": false,
            "false_recommendation_rate": false / observed if observed else None,
            "search_cost": cost,
            "recorded_operator_interventions": intervention_count
            if intervention_coverage == len(experiment_ids)
            else None,
            "operator_intervention_coverage": intervention_coverage / len(experiment_ids),
            "unit_improvements": unit_improvements,
        }
    decision = "insufficient_evidence"
    intervals = {}
    if not reasons:
        new = results["new"]
        random = np.random.default_rng(42)
        for arm in ("old", "simple_rule"):
            differences = np.array(new["unit_improvements"]) - results[arm]["unit_improvements"]
            samples = differences[
                random.integers(0, len(differences), (4000, len(differences)))
            ].mean(axis=1)
            # Two predeclared comparisons use a Bonferroni-adjusted interval.
            intervals[arm] = np.quantile(samples, [0.0125, 0.9875]).tolist()
        n = new["observed_recommendations"]
        if n:
            proportion, z = new["later_failures"] / n, 1.96
            upper = (
                proportion
                + z * z / (2 * n)
                + z * np.sqrt(proportion * (1 - proportion) / n + z * z / (4 * n * n))
            ) / (1 + z * z / n)
        else:
            upper = 1.0
        new["false_rate_upper_bound"] = float(upper)
        if (
            all(interval[0] > 0 for interval in intervals.values())
            and upper <= study["maximum_false_rate"]
        ):
            decision = "recommend"
        elif any(interval[1] <= 0 for interval in intervals.values()):
            decision = "keep_old"
        else:
            reasons.append("Strategy gain or later false-recommendation bound is inconclusive")
    record = {
        **study,
        "results": results,
        "decision": decision,
        "reasons": reasons,
        "gain_intervals": intervals,
        "evaluated_at": now(),
        "status": "evaluated",
    }
    return record


def evaluate_study(service: "Service", study_id: str) -> Any:
    def update(state: dict[str, Any]) -> Any:
        record = _study_report(state, study_id)
        state["strategies"][study_id] = record
        return record

    return service.store.update(update)


def promote_strategy(
    service: "Service", study_id: str, expected_default_version: str, *, key: str
) -> Any:
    """Recheck current evidence before changing a verified application default."""

    def update(state: dict[str, Any]) -> Any:
        study = state["strategies"][study_id]
        application_ids = study.get("application_ids", [])
        if not application_ids:
            raise ValueError("Study lacks a frozen application scope for policy promotion")
        for app_id in application_ids:
            current = default_policy(state, app_id)
            if current.version != expected_default_version or current.model_dump(
                mode="json"
            ) != study.get("default_baselines", {}).get(app_id):
                raise Conflict("Default strategy changed since the study was frozen")
        if not study.get("promotion_eligible"):
            raise ValueError(
                "Study did not bind the current default, simple rule and reproducible new policy"
            )
        report = _study_report(state, study_id)
        if report["decision"] != "recommend":
            raise ValueError("Strategy lacks independently sufficient evidence for promotion")
        policy = SearchPolicy.model_validate(study["policies"]["new"])
        previous = {
            app_id: state.get("default_strategies", {}).get(
                app_id, {"policy": default_policy(state, app_id).model_dump(mode="json")}
            )
            for app_id in application_ids
        }
        record = {
            "policy": policy.model_dump(mode="json"),
            "source_study": study_id,
            "previous": previous,
            "application_ids": application_ids,
            "provenance": {
                app_id: state["applications"][app_id]["declaration"]["provenance"]
                for app_id in application_ids
            },
            "changed_at": now(),
        }
        state["strategies"][study_id] = report
        state["default_strategy"] = record
        state.setdefault("default_strategies", {}).update(
            {app_id: record for app_id in application_ids}
        )
        return record

    return service.store.update(
        update,
        key=f"promote-strategy:{key}",
        payload={"study_id": study_id, "expected_default_version": expected_default_version},
    )
