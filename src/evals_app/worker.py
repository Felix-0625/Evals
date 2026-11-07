"""Isolated synchronous Inspect execution; its .eval log owns sample facts."""

import json
import os
import sys
from pathlib import Path
from typing import Any

import psutil
from inspect_ai.dataset import Sample
from inspect_ai.model import ChatMessageAssistant, ChatMessageTool, ModelOutput
from inspect_ai.scorer import Score, Scorer, Target, scorer
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import ToolCall, ToolCallError

from evals_app.adapters import execute
from evals_app.analysis import business_scanner
from evals_app.build import build_info
from evals_app.coordination import CallDenied, CallGate
from evals_app.models import (
    Application,
    Case,
    Dimension,
    Evidence,
    ExecutionPlan,
    Judgment,
    Observation,
    SemanticAnalyzer,
    Status,
    fingerprint,
)
from evals_app.scoring import judge
from inspect_ai import Epochs, Task, eval


def external_messages(
    observation: dict[str, Any] | None,
) -> list[ChatMessageAssistant | ChatMessageTool]:
    """Import observed tool records into the existing Viewer message components."""
    messages: list[ChatMessageAssistant | ChatMessageTool] = []
    if observation is None:
        return messages
    for call in observation.get("facts", {}).get("process", {}).get("calls", []):
        if (
            not isinstance(call, dict)
            or not all(call.get(k) for k in ("id", "name"))
            or not isinstance(call.get("arguments"), dict)
        ):
            continue
        metadata = {"origin": "external_application", "original_call_id": call["id"]}
        messages.append(
            ChatMessageAssistant(
                content="External application tool call (recorded by adapter)",
                source="input",
                metadata=metadata,
                tool_calls=[
                    ToolCall(id=call["id"], function=call["name"], arguments=call["arguments"])
                ],
            )
        )
        observed = "result" in call
        result = call.get("result")
        content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        messages.append(
            ChatMessageTool(
                content=content
                if observed
                else "Tool result not exposed by the external application",
                source="input",
                metadata={**metadata, "result_observed": observed},
                tool_call_id=call["id"],
                function=call["name"],
                error=ToolCallError(type="unknown", message=str(call["error"]))
                if call.get("error")
                else None,
            )
        )
    return messages


@solver
def application_solver(
    application: dict[str, Any],
    config: dict[str, Any],
    context: dict[str, Any],
    retries: int,
    retryable: list[str],
    coordination_dir: str | None = None,
) -> Solver:
    contract = Application.model_validate(application)

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        case = Case.model_validate(state.metadata["evals_case"])
        result = await execute(
            contract,
            case,
            config,
            {**context, "sample_id": str(state.sample_id), "epoch": state.epoch},
            retries,
            retryable,
            CallGate(Path(coordination_dir)) if coordination_dir else None,
        )
        state.metadata["evals_observation"] = result["observation"]
        state.metadata["evals_attempts"] = result["attempts"]
        state.metadata["evals_blocked_reason"] = result["blocked_reason"]
        state.metadata["evals_config_fingerprint"] = fingerprint(config)
        output = (
            result["observation"]["output"] if result["observation"] else "Execution unavailable"
        )
        state.output = ModelOutput.from_content("mockllm/model", output)
        state.messages.extend(external_messages(result["observation"]))
        state.messages.append(ChatMessageAssistant(content=output))
        state.completed = True
        return state

    return solve


@scorer(metrics=[])
def business_scores(
    application: dict[str, Any],
    semantic_grader: dict[str, Any] | None = None,
    grading_dir: str | None = None,
) -> Scorer:
    contract = Application.model_validate(application)
    definition = SemanticAnalyzer.model_validate(semantic_grader) if semantic_grader else None
    if definition and grading_dir is None:
        raise ValueError("Semantic grading requires an owning budget gate")
    grader = None

    async def semantic_grade(
        state: TaskState, case: Case, observation: Observation | None, judgments: list[Judgment]
    ) -> list[Judgment]:
        nonlocal grader
        if definition is None or grading_dir is None:
            return judgments
        rule = next((rule for rule in case.rules if rule.operator == "semantic"), None)
        if rule is None or rule.dimension not in contract.observable:
            return judgments
        from evals_app.semantic import evidence_row
        from evals_app.semantic_worker import create_gated_model, scan_row, semantic_scanner

        row = evidence_row(
            contract,
            definition,
            observation,
            str(state.sample_id),
            state.epoch,
            required_source=(rule.path, rule.source),
        )
        version = f"semantic:{definition.id}:{definition.version}"
        grade: dict[str, Any] = {
            "scorer_version": version,
            "model": definition.model_version,
            "calibration_id": definition.calibration_id,
            "dimension": rule.dimension,
        }
        evidence = []
        if row["text"] is None:
            status, reason = Status.UNVERIFIABLE, "Authorized semantic evidence is missing"
        else:
            try:
                if grader is None:
                    model = create_gated_model(definition, Path(grading_dir))
                    grader = (model, semantic_scanner(definition, model))
                result = await scan_row(definition, *grader, row, grading=True)
                grade["result"] = result.model_dump(mode="json")
                evidence = [Evidence.model_validate(reference) for reference in row["references"]]
                if result.value == "unverifiable":
                    status, reason = Status.UNVERIFIABLE, "Semantic model could not verify the rule"
                else:
                    status = Status.PASS if result.value == rule.expected else Status.FAIL
                    reason = f"Semantic verdict {result.value}; expected {rule.expected}"
            except CallDenied as error:
                status, reason = Status.NOT_RUN, str(error)
            except Exception as error:
                status, reason = (
                    Status.GRADING_ERROR,
                    f"Semantic grader error: {type(error).__name__}",
                )
        state.metadata["evals_semantic_grade"] = {**grade, "status": status, "reason": reason}
        return [
            Judgment(
                dimension=rule.dimension,
                status=status,
                reason=reason,
                scorer_version=version,
                evidence=evidence,
            )
            if j.dimension == rule.dimension
            else j
            for j in judgments
        ]

    async def score(state: TaskState, target: Target) -> Score:
        case = Case.model_validate(state.metadata["evals_case"])
        raw = state.metadata.get("evals_observation")
        observation = Observation.model_validate(raw) if raw else None
        judgments = judge(case, contract, observation)
        if state.metadata.get("evals_blocked_reason") and not state.metadata.get("evals_attempts"):
            judgments = [
                j.model_copy(
                    update={
                        "status": Status.NOT_RUN,
                        "reason": state.metadata["evals_blocked_reason"],
                    }
                )
                if j.status != Status.NOT_APPLICABLE
                else j
                for j in judgments
            ]
        else:
            judgments = await semantic_grade(state, case, observation, judgments)
        state.metadata["evals_judgments"] = [j.model_dump(mode="json") for j in judgments]
        values = {
            j.dimension.value: (1 if j.status == Status.PASS else 0)
            for j in judgments
            if j.status in (Status.PASS, Status.FAIL)
        }
        metadata = {"judgments": [j.model_dump(mode="json") for j in judgments]}
        if "evals_semantic_grade" in state.metadata:
            metadata["semantic_grade"] = state.metadata["evals_semantic_grade"]
        return Score(
            value=values,
            explanation="\n".join(f"{j.dimension}: {j.status} — {j.reason}" for j in judgments),
            metadata=metadata,
        )

    return score


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    if job.get("build") is not None and job["build"] != build_info():
        raise ValueError("Worker integration code differs from the frozen experiment build")
    plan = ExecutionPlan.model_validate(job["plan"])
    cases = [Case.model_validate(c) for c in job["cases"]]
    config_fingerprint = fingerprint(job["config"])
    context = {
        "experiment_id": job["experiment_id"],
        "candidate_id": job["candidate_id"],
        "application_version": plan.application.version,
    }
    task = Task(
        name="evals-application",
        dataset=[
            Sample(
                id=c.id,
                input=c.input,
                metadata={
                    "evals_case": c.model_dump(mode="json"),
                    "evals_config_fingerprint": config_fingerprint,
                    # Keep the eight dispositions when a native failure prevents scoring.
                    "evals_judgments": [
                        j.model_dump(mode="json") for j in judge(c, plan.application, None)
                    ],
                    **context,
                },
            )
            for c in cases
        ],
        solver=application_solver(
            plan.application.model_dump(mode="json"),
            job["config"],
            context,
            plan.platform_retries,
            plan.retryable,
            job.get("coordination_dir"),
        ),
        scorer=business_scores(
            plan.application.model_dump(mode="json"),
            plan.semantic_grader.model_dump(mode="json") if plan.semantic_grader else None,
            job.get("grading_dir"),
        ),
        epochs=Epochs(plan.epochs),
        metadata={
            **context,
            "plan_fingerprint": job["plan_fingerprint"],
            "build": job.get("build") or build_info(),
            "provenance": plan.application.provenance,
            "dimensions": [d.value for d in Dimension],
        },
    )
    logs = eval(
        task,
        model="mockllm/model",
        log_dir=job["log_dir"],
        display="none",
        max_samples=1,
        fail_on_error=False,
        scanner=[business_scanner()] if job.get("scan", False) else None,
    )
    if len(logs) != 1 or logs[0].status != "success":
        failures = [
            {
                "status": item.status,
                "location": item.location,
                "error": item.error.model_dump(mode="json") if item.error else None,
            }
            for item in logs
        ]
        raise RuntimeError(f"Inspect run did not complete: {failures}")
    log = logs[0]
    groups = {c.id: c.group for c in cases}
    trials = []
    for sample in log.samples or []:
        metadata = sample.metadata or {}
        judgments = metadata.get("evals_judgments")
        if judgments is None and sample.scores:
            judgments = (next(iter(sample.scores.values())).metadata or {}).get("judgments")
        if judgments is None:
            judgments = [
                j.model_dump(mode="json")
                for j in judge(
                    next(c for c in cases if c.id == str(sample.id)), plan.application, None
                )
            ]
        attempts = metadata.get("evals_attempts", [])
        trials.append(
            {
                "sample_id": str(sample.id),
                "group": groups[str(sample.id)],
                "epoch": sample.epoch,
                "attempt_id": attempts[-1]["attempt_id"] if attempts else "missing",
                "candidate_id": job["candidate_id"],
                "run_id": log.eval.run_id,
                "config_fingerprint": config_fingerprint,
                "application_version": plan.application.version,
                "observation": metadata.get("evals_observation"),
                "judgments": judgments,
                "attempts": attempts,
                "log": log.location,
            }
        )
    if len(trials) != len(cases) * plan.epochs:
        raise RuntimeError("Inspect did not return the complete planned sample/epoch grid")
    return {"log": log.location, "run_id": log.eval.run_id, "trials": trials}


def main() -> None:
    path = Path(sys.argv[1])
    path.with_suffix(".identity.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "create_time": psutil.Process().create_time(),
                "job_file": str(path.resolve()),
            }
        ),
        "utf-8",
    )
    job = json.loads(path.read_text("utf-8"))
    try:
        result = run_job(job)
    except Exception as error:
        result = {"error": type(error).__name__, "detail": str(error)}
    path.with_suffix(".result.json").write_text(json.dumps(result, allow_nan=False), "utf-8")
    if "error" in result:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
