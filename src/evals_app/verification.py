"""Paired cluster bootstrap with hard constraints and independent quality guards."""

from collections import defaultdict
from typing import Any

import numpy as np

from evals_app.models import Dimension, Plan, Status, Trial, fingerprint
from evals_app.scoring import coverage


class InsufficientEvidence(ValueError):
    pass


def require_semantic_coverage(plan: Plan, trials: list[Trial]) -> None:
    grader = plan.semantic_grader
    if grader is None:
        return
    dimensions = {
        case.id: {rule.dimension for rule in case.rules if rule.operator == "semantic"}
        for case in plan.cases
    }
    judged = [
        judgment
        for trial in trials
        for judgment in trial.judgments
        if judgment.dimension in dimensions[trial.sample_id]
    ]
    if (
        judged
        and sum(j.status in {Status.PASS, Status.FAIL} for j in judged) / len(judged)
        < grader.minimum_coverage
    ):
        raise InsufficientEvidence(
            "Semantic business grading coverage is below the frozen threshold"
        )


def summary(trials: list[Trial]) -> dict[str, Any]:
    grouped: dict[str, list[Trial]] = defaultdict(list)
    repeated: dict[str, list[Trial]] = defaultdict(list)
    for trial in trials:
        grouped[trial.group].append(trial)
        repeated[trial.sample_id].append(trial)
    repetitions = []
    for sample_id, rows in sorted(repeated.items()):
        results = [j for row in rows for j in row.judgments if j.dimension == Dimension.RESULT]
        statuses = {j.status for j in results}
        repetitions.append(
            {
                "sample_id": sample_id,
                "group": rows[0].group,
                "epochs": [row.epoch for row in rows],
                "result": coverage(results),
                "mixed_outcomes": Status.PASS in statuses and Status.FAIL in statuses,
                "missing_results": len(results) != len(rows)
                or any(s not in (Status.PASS, Status.FAIL) for s in statuses),
                "logs": sorted({row.log for row in rows}),
            }
        )
    return {
        **_metrics(trials),
        "by_group": {name: _metrics(rows) for name, rows in sorted(grouped.items())},
        "repetitions": repetitions,
        "mixed_outcome_samples": [r["sample_id"] for r in repetitions if r["mixed_outcomes"]],
        "missing_result_samples": [r["sample_id"] for r in repetitions if r["missing_results"]],
    }


def _metrics(trials: list[Trial]) -> dict[str, Any]:
    all_judgments = [j for trial in trials for j in trial.judgments]
    costs = [attempt.get("cost") for trial in trials for attempt in trial.attempts]
    unknown = not costs or any(cost is None for cost in costs)
    known_cost = sum(float(cost) for cost in costs if cost is not None)
    successes = sum(
        any(j.dimension == Dimension.RESULT and j.status == Status.PASS for j in trial.judgments)
        for trial in trials
    )
    latencies = [sum(a["latency"] for a in trial.attempts) for trial in trials if trial.attempts]
    return {
        "coverage": coverage(all_judgments),
        "dimensions": {
            d.value: coverage([j for j in all_judgments if j.dimension == d]) for d in Dimension
        },
        "independent_samples": len({t.sample_id for t in trials}),
        "groups": len({t.group for t in trials}),
        "rounds": len(trials),
        "successes": successes,
        "cost": None if unknown else known_cost,
        "unknown_cost_attempts": sum(c is None for c in costs),
        "cost_per_success": None if unknown or not successes else known_cost / successes,
        "latency_p95": float(np.quantile(latencies, 0.95)) if latencies else None,
    }


def quality(trials: list[Trial]) -> float:
    judgments = [
        j
        for t in trials
        for j in t.judgments
        if j.dimension != Dimension.COST and j.status != Status.NOT_APPLICABLE
    ]
    if not judgments or any(j.status not in (Status.PASS, Status.FAIL) for j in judgments):
        raise InsufficientEvidence("Quality guard requires complete applicable judgments")
    return sum(j.status == Status.PASS for j in judgments) / len(judgments)


def metric(trials: list[Trial], objective: str) -> float:
    if objective == "quality":
        return quality(trials)
    value = _metrics(trials)[objective]
    if value is None or not np.isfinite(value):
        raise InsufficientEvidence(f"Cannot calculate {objective}")
    return float(value)


def compare(
    plan: Plan, baseline: list[Trial], candidate: list[Trial], *, phase: str = "holdout"
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "phase": phase,
        "decision": "insufficient_evidence",
        "reasons": [],
        "baseline": summary(baseline),
        "candidate": summary(candidate),
        "plan_fingerprint": fingerprint(plan),
        "regressions": [],
    }
    try:
        base = {(t.sample_id, t.epoch): t for t in baseline}
        cand = {(t.sample_id, t.epoch): t for t in candidate}
        expected_cases = {c.id: c for c in plan.cases if c.split == phase}
        expected = {
            (sample_id, epoch)
            for sample_id in expected_cases
            for epoch in range(1, plan.epochs + 1)
        }
        if (
            set(base) != expected
            or set(cand) != expected
            or len(base) != len(baseline)
            or len(cand) != len(candidate)
        ):
            raise InsufficientEvidence("Incomplete or duplicate paired sample/epoch grid")
        for t in baseline + candidate:
            if (
                t.application_version != plan.application.version
                or t.group != expected_cases[t.sample_id].group
            ):
                raise InsufficientEvidence("Frozen execution identity or grouping mismatch")
        groups: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for key in base:
            groups[base[key].group].append(key)
        if len(groups) < plan.minimum_groups:
            raise InsufficientEvidence("Too few independent business groups")
        hard = [
            j
            for t in candidate
            for j in t.judgments
            if j.dimension in plan.hard_dimensions and j.status != Status.NOT_APPLICABLE
        ]
        if any(j.status == Status.FAIL for j in hard):
            report.update(
                decision="reject", reasons=["Hard business or security constraint failed"]
            )
            return report
        if not hard or any(j.status != Status.PASS for j in hard):
            raise InsufficientEvidence("Hard constraint evidence is incomplete")
        for trials in (baseline, candidate):
            require_semantic_coverage(plan, trials)
            judgments = [j for t in trials for j in t.judgments]
            if any(j.critical and j.status not in (Status.PASS, Status.FAIL) for j in judgments):
                raise InsufficientEvidence("Critical check is missing")
            if (coverage(judgments)["coverage"] or 0) < plan.minimum_coverage:
                raise InsufficientEvidence("Coverage is below the frozen threshold")
        for key, original in base.items():
            for old, new in zip(original.judgments, cand[key].judgments, strict=True):
                if old.status == Status.PASS and new.status == Status.FAIL:
                    report["regressions"].append(
                        {
                            "sample_id": key[0],
                            "epoch": key[1],
                            "dimension": new.dimension,
                            "log": cand[key].log,
                        }
                    )
        direction = 1 if plan.objective == "quality" else -1
        gain = direction * (metric(candidate, plan.objective) - metric(baseline, plan.objective))
        quality_change = quality(candidate) - quality(baseline)
        random = np.random.default_rng(plan.seed)
        names = list(groups)
        gains, qualities = [], []
        for _ in range(plan.bootstrap_samples):
            indices = random.integers(0, len(names), len(names))
            keys = [key for index in indices for key in groups[names[index]]]
            b, c = [base[k] for k in keys], [cand[k] for k in keys]
            gains.append(direction * (metric(c, plan.objective) - metric(b, plan.objective)))
            qualities.append(quality(c) - quality(b))
        alpha = (1 - plan.confidence) / 2
        gain_ci = np.quantile(gains, [alpha, 1 - alpha]).tolist()
        quality_ci = np.quantile(qualities, [alpha, 1 - alpha]).tolist()
        report.update(
            gain=gain,
            gain_interval=gain_ci,
            quality_change=quality_change,
            quality_interval=quality_ci,
            method="paired group bootstrap",
            confidence=plan.confidence,
            bootstrap_samples=plan.bootstrap_samples,
        )
        if quality_ci[1] < -plan.quality_tolerance or gain_ci[1] < plan.minimum_gain:
            report.update(decision="reject", reasons=["Quality guard or minimum gain is not met"])
        elif quality_ci[0] >= -plan.quality_tolerance and gain_ci[0] >= plan.minimum_gain:
            report.update(
                decision="recommend", reasons=["Hard constraints, quality and gain passed"]
            )
        else:
            report["reasons"] = ["Intervals do not support the frozen adoption thresholds"]
    except (InsufficientEvidence, ValueError, TypeError) as error:
        report["reasons"] = [str(error)]
    return report
