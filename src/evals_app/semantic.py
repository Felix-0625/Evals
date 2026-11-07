"""Calibration and evidence projection for semantic analysis and business grading."""

import json
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field

from evals_app.models import (
    Application,
    Contract,
    Evidence,
    Observation,
    Plan,
    SemanticAnalyzer,
    Trial,
    fingerprint,
    now,
)
from evals_app.scoring import resolve, shareable_paths
from evals_app.store import Conflict

if TYPE_CHECKING:
    from evals_app.service import Service


class SemanticValidationCase(Contract):
    id: str = Field(min_length=1)
    group: str = Field(min_length=1)
    text: str | None = Field(default=None, max_length=100000)
    reference: Evidence
    expected: Literal["yes", "no", "unverifiable"]


Role = Literal["analysis", "grading"]


def semantic_definition(plan: Plan, role: Role) -> SemanticAnalyzer | None:
    return plan.semantic_analyzer if role == "analysis" else plan.semantic_grader


def calibration_collection(role: Role) -> str:
    return "analyzer_calibrations" if role == "analysis" else "grader_calibrations"


def check_calibration(state: dict[str, Any], plan: Plan, build: dict[str, str]) -> None:
    for role in ("analysis", "grading"):
        definition = semantic_definition(plan, role)
        if definition is None:
            continue
        record = state.get(calibration_collection(role), {}).get(definition.calibration_id)
        if (
            not record
            or record.get("status") != "passed"
            or record["definition_fingerprint"] != fingerprint(definition)
            or record["application_id"] != plan.application.id
            or record["build"] != build
        ):
            raise ValueError(
                f"Semantic {role} requires a matching completed calibration before execution"
            )
        if record["provenance"] == "fixture" and plan.application.provenance != "fixture":
            raise ValueError(
                "Mock calibration cannot authorize real-application semantic judgments"
            )


def schedule_calibration(
    service: "Service",
    experiment_id: str,
    cases: list[SemanticValidationCase],
    *,
    key: str,
    role: Role = "analysis",
) -> Any:
    if len(cases) < 3 or {c.expected for c in cases} != {"yes", "no", "unverifiable"}:
        raise ValueError(
            "Semantic calibration requires positive, negative and missing-evidence labels"
        )
    if len({c.id for c in cases}) != len(cases) or not any(c.text is None for c in cases):
        raise ValueError("Calibration requires unique IDs and a missing-evidence boundary")
    if any(c.text is None and c.expected != "unverifiable" for c in cases):
        raise ValueError("Missing evidence cannot be labeled as a confirmed positive or negative")

    def update(state: dict[str, Any]) -> Any:
        experiment = state["experiments"][experiment_id]
        plan = Plan.model_validate(experiment["plan"])
        definition = semantic_definition(plan, role)
        if (
            definition is None
            or experiment["status"] not in {"draft", "blocked"}
            or experiment.get("runs")
        ):
            raise Conflict("Calibration requires an unexecuted experiment with a frozen definition")
        if experiment["build"] != service.build:
            raise Conflict("Calibration code differs from the prepared experiment")
        texts = [c.text for c in cases if c.text is not None]
        if (
            len(set(texts)) != len(texts)
            or {c.group for c in cases} & {c.group for c in plan.cases}
            or set(texts) & {c.input for c in plan.cases}
        ):
            raise ValueError(
                "Calibration examples must be distinct from the frozen evaluation data"
            )
        if any(c.text and len(c.text) > definition.max_input_chars for c in cases):
            raise ValueError("Calibration example exceeds the frozen input limit")
        if definition.deployment == "external" and any(
            c.reference.scope != "shareable" for c in cases
        ):
            raise ValueError("Local calibration evidence cannot enter an external judge")
        records = state.setdefault(calibration_collection(role), {})
        dataset_hash = fingerprint([c.model_dump(mode="json") for c in cases])
        existing = records.get(definition.calibration_id)
        if existing:
            if (
                existing["definition_fingerprint"] != fingerprint(definition)
                or existing["dataset_fingerprint"] != dataset_hash
                or existing["application_id"] != plan.application.id
                or existing["build"] != service.build
            ):
                raise Conflict(
                    "Calibration artifacts are immutable; choose a new definition/artifact"
                )
            if existing["status"] == "passed":
                return {
                    "id": experiment_id,
                    "status": "calibrated",
                    "artifact": definition.calibration_id,
                }
            if existing["status"] == "queued":
                raise Conflict("The calibration artifact already has a queued or running owner")
        records[definition.calibration_id] = {
            "id": definition.calibration_id,
            "role": role,
            "status": "queued",
            "application_id": plan.application.id,
            "experiment_id": experiment_id,
            "definition_fingerprint": fingerprint(definition),
            "dataset_fingerprint": dataset_hash,
            "dataset_version": definition.calibration_dataset_version,
            "build": service.build,
        }
        experiment.update(
            status="queued",
            control="run",
            pending_semantic_calibration=True,
            semantic_calibration_role=role,
            semantic_calibration_cases=[c.model_dump(mode="json") for c in cases],
        )
        return {
            "id": experiment_id,
            "status": "calibration_queued",
            "artifact": definition.calibration_id,
        }

    result = service.store.update(
        update,
        key=(
            f"semantic-calibration:{experiment_id}:{key}"
            if role == "analysis"
            else f"semantic-grader-calibration:{experiment_id}:{key}"
        ),
        payload=[c.model_dump(mode="json") for c in cases],
    )
    service.runtime.wake.set()
    return result


def run_calibration(service: "Service", experiment_id: str, plan: Plan) -> None:
    state = service.store.read()
    experiment = state["experiments"][experiment_id]
    role: Role = experiment.get("semantic_calibration_role", "analysis")
    definition = semantic_definition(plan, role)
    if definition is None:
        raise ValueError("Missing semantic calibration definition")
    collection = calibration_collection(role)

    def owned_record(state: dict[str, Any]) -> dict[str, Any]:
        record = state.get(collection, {}).get(definition.calibration_id)
        if not record or record.get("experiment_id", experiment_id) != experiment_id:
            raise Conflict("Semantic calibration is owned by a different experiment")
        return record

    owned_record(state)
    service._phase(
        experiment_id, "semantic_calibration" if role == "analysis" else "grader_calibration"
    )
    cases = [
        SemanticValidationCase.model_validate(c) for c in experiment["semantic_calibration_cases"]
    ]
    rows = [
        {"id": c.id, "text": c.text, "scope": c.reference.scope, "expected": c.expected}
        for c in cases
    ]
    result = service.runtime.analyze(experiment_id, plan, rows, role=role)
    correct = sum(r["correct"] is True for r in result["rows"])
    passed = correct / len(cases) >= definition.minimum_accuracy

    def update(state: dict[str, Any]) -> None:
        record = owned_record(state)
        record.update(
            status="passed" if passed else "failed",
            expected=len(cases),
            correct=correct,
            minimum_accuracy=definition.minimum_accuracy,
            artifact=result["artifact"],
            provenance=result["provenance"],
            completed_at=now(),
            results=result["rows"],
        )
        state["experiments"][experiment_id].update(
            status="draft",
            stage="prepared",
            pending_semantic_calibration=False,
            reason=None if passed else "Semantic calibration did not meet its frozen threshold",
        )

    service.store.update(update)


def fail_calibration(
    state: dict[str, Any],
    experiment: dict[str, Any],
    status: Literal["failed", "cancelled"],
    error: str,
) -> None:
    if not experiment.get("pending_semantic_calibration"):
        return
    role: Role = experiment.get("semantic_calibration_role", "analysis")
    definition = semantic_definition(Plan.model_validate(experiment["plan"]), role)
    if definition:
        record = state.get(calibration_collection(role), {}).get(definition.calibration_id)
        if record and record.get("experiment_id", experiment["id"]) == experiment["id"]:
            record.update(status=status, error=error)
    if status == "cancelled":
        state["experiments"][experiment["id"]]["pending_semantic_calibration"] = False


def permitted_row(plan: Plan, trial: Trial) -> dict[str, Any]:
    definition = plan.semantic_analyzer
    if definition is None:
        raise ValueError("Missing semantic analyzer")
    return evidence_row(
        plan.application, definition, trial.observation, trial.sample_id, trial.epoch
    )


def evidence_row(
    application: Application,
    definition: SemanticAnalyzer,
    observation: Observation | None,
    sample_id: str,
    epoch: int,
    *,
    required_source: tuple[str, str] | None = None,
) -> dict[str, Any]:
    external = definition.deployment == "external"
    if external and application.evidence_scope != "shareable":
        raise ValueError("Application does not authorize external semantic analysis")
    values: list[dict[str, Any]] = []
    allowed: set[str] = set()
    if observation:
        if application.evidence_scope == "shareable":
            allowed = shareable_paths(
                {path: ref.scope for path, ref in observation.evidence.items()}
            )
        for path in definition.evidence_paths:
            reference = observation.evidence.get(path)
            if reference is None or (external and path not in allowed):
                continue
            if (
                required_source
                and required_source[0] == path
                and reference.source != required_source[1]
            ):
                continue
            try:
                value = observation.output if path == "output" else resolve(observation.facts, path)
            except KeyError:
                continue
            values.append(
                {"path": path, "value": value, "reference": reference.model_dump(mode="json")}
            )
    complete = len(values) == len(set(definition.evidence_paths))
    scope = "shareable" if values and all(v["path"] in allowed for v in values) else "local"
    return {
        "id": f"{sample_id}:epoch:{epoch}",
        "sample_id": sample_id,
        "epoch": epoch,
        "text": json.dumps({"evidence": values}, ensure_ascii=False) if complete else None,
        "scope": scope,
        "references": [v["reference"] for v in values],
    }


def analyze_trials(
    service: "Service", experiment_id: str, plan: Plan, trials: list[Trial]
) -> list[dict[str, Any]]:
    definition = plan.semantic_analyzer
    if definition is None:
        return []
    check_calibration(service.store.read(), plan, service.build)
    rows = [permitted_row(plan, trial) for trial in trials]
    result = service.runtime.analyze(experiment_id, plan, rows)
    service.runtime.patch(experiment_id, semantic_result=result)
    covered = sum(r["result"]["value"] in {"yes", "no"} for r in result["rows"])
    if not rows or covered / len(rows) < definition.minimum_coverage:
        raise ValueError("Semantic evidence coverage is below the frozen threshold")
    findings = []
    for trial, row, detected in zip(trials, rows, result["rows"], strict=True):
        if detected["result"]["value"] != "yes":
            continue
        findings.append(
            {
                "fact": f"Semantic scanner flagged: {definition.question}",
                "hypothesis": detected["result"].get("explanation"),
                "test": "Confirm this model hypothesis using independent business evidence",
                "sample_id": trial.sample_id,
                "epoch": trial.epoch,
                "log": trial.log,
                "candidate_id": trial.candidate_id,
                "config_fingerprint": trial.config_fingerprint,
                "analyzer_version": definition.version,
                "model": definition.model_version,
                "references": row["references"],
                "scope": row["scope"],
                "scanner_references": detected["result"]["references"],
                "artifact": result["artifact"],
            }
        )
    return findings
