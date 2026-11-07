"""Reconstruct application trials from their authoritative Inspect sample logs."""

import json
from pathlib import Path
from typing import Any

from inspect_ai.log import EvalSample, read_eval_log

from evals_app.models import Dimension, Status, Trial


def _legacy_failed_trial(run: dict[str, Any], run_id: str, sample: EvalSample) -> Trial:
    """Recover an unscored sample from the original worker receipt for a legacy run."""
    receipt_path = Path(run["log"]).with_name("job.result.json")
    if not receipt_path.is_file():
        raise ValueError("Unscored native sample has no retained worker receipt")
    receipt = json.loads(receipt_path.read_text("utf-8"))
    if receipt.get("log") != run["log"] or receipt.get("run_id") != run_id:
        raise ValueError("Worker receipt does not belong to the native log")
    matches = [
        item
        for item in receipt["trials"]
        if item["sample_id"] == str(sample.id) and item["epoch"] == sample.epoch
    ]
    if len(matches) != 1:
        raise ValueError("Worker receipt lacks a unique native sample disposition")
    trial = Trial.model_validate(matches[0])
    if (
        trial.candidate_id != run["candidate_id"]
        or trial.run_id != run_id
        or trial.log != run["log"]
        or trial.group != sample.metadata["evals_case"]["group"]
        or trial.application_version != sample.metadata["application_version"]
        or len(trial.judgments) != len(Dimension)
        or {j.dimension for j in trial.judgments} != set(Dimension)
        or any(
            j.status not in {Status.UNVERIFIABLE, Status.NOT_APPLICABLE} for j in trial.judgments
        )
    ):
        raise ValueError("Worker receipt does not preserve the unscored native sample")
    return trial


def load_trials(run: dict[str, Any]) -> list[Trial]:
    if "trials" in run:
        return [Trial.model_validate(trial) for trial in run["trials"]]
    log = read_eval_log(run["log"])
    trials = []
    for sample in log.samples or []:
        metadata = sample.metadata or {}
        judgments = metadata.get("evals_judgments")
        if judgments is None and sample.scores:
            judgments = (next(iter(sample.scores.values())).metadata or {}).get("judgments")
        if judgments is None:
            if sample.error:
                trials.append(_legacy_failed_trial(run, log.eval.run_id, sample))
                continue
            raise ValueError("Registered application log lacks its eight-dimensional judgments")
        attempts = metadata.get("evals_attempts", [])
        trials.append(
            Trial(
                sample_id=str(sample.id),
                group=metadata["evals_case"]["group"],
                epoch=sample.epoch,
                attempt_id=attempts[-1]["attempt_id"] if attempts else "missing",
                candidate_id=run["candidate_id"],
                run_id=log.eval.run_id,
                config_fingerprint=metadata["evals_config_fingerprint"],
                application_version=metadata["application_version"],
                observation=metadata.get("evals_observation"),
                judgments=judgments,
                attempts=attempts,
                log=run["log"],
            )
        )
    expected = {(row["sample_id"], row["epoch"]) for row in run["trial_index"]}
    if len(trials) != len(expected) or {(t.sample_id, t.epoch) for t in trials} != expected:
        raise ValueError("Registered log no longer matches its sample/epoch index")
    return trials
