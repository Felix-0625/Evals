"""Validate complete execution identity before consuming worker or optimizer results."""

from typing import Any

from evals_app.models import (
    Application,
    Case,
    Dimension,
    SemanticAnalyzer,
    Status,
    Trial,
    fingerprint,
)


def validate_execution(
    trials: list[Trial],
    cases: list[Case],
    application: Application,
    config: dict[str, Any],
    candidate_id: str,
    epochs: int,
    *,
    run_id: str | None = None,
    log: str | None = None,
    grader: SemanticAnalyzer | None = None,
) -> None:
    planned = {case.id: case for case in cases}
    expected = {(case.id, epoch) for case in cases for epoch in range(1, epochs + 1)}
    if not expected or len(planned) != len(cases):
        raise ValueError("Execution requires a nonempty, unique planned batch")
    if len(trials) != len(expected) or {(t.sample_id, t.epoch) for t in trials} != expected:
        raise ValueError("Incomplete, foreign or duplicate sample/epoch results")
    if len({t.run_id for t in trials}) != 1 or len({t.log for t in trials}) != 1:
        raise ValueError("Results mix different execution runs or logs")
    config_hash = fingerprint(config)
    for trial in trials:
        case = planned[trial.sample_id]
        if (
            trial.candidate_id != candidate_id
            or trial.config_fingerprint != config_hash
            or trial.application_version != application.version
            or trial.group != case.group
            or not trial.run_id
            or not trial.log
            or (run_id is not None and trial.run_id != run_id)
            or (log is not None and trial.log != log)
            or (
                trial.observation is not None
                and trial.observation.application_version != application.version
            )
        ):
            raise ValueError("Result identity differs from the frozen execution request")
        if len(trial.judgments) != len(Dimension) or {j.dimension for j in trial.judgments} != set(
            Dimension
        ):
            raise ValueError("Execution lost or duplicated a dimension judgment")
        rules = {rule.dimension: rule for rule in case.rules}
        for judgment in trial.judgments:
            rule = rules.get(judgment.dimension)
            if judgment.critical != bool(rule and rule.critical) or (
                (judgment.status == Status.NOT_APPLICABLE) != (rule is None)
            ):
                raise ValueError(
                    "Result applicability or critical flag differs from the frozen rubric"
                )
            if (
                rule
                and rule.operator == "semantic"
                and judgment.status in {Status.PASS, Status.FAIL}
            ):
                if (
                    not grader
                    or judgment.scorer_version != f"semantic:{grader.id}:{grader.version}"
                ):
                    raise ValueError("Semantic result differs from the frozen scorer")
                if not any(reference.source == rule.source for reference in judgment.evidence):
                    raise ValueError("Conclusive semantic result lacks the required evidence")
