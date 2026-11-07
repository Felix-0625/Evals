"""Audit persisted runs and GEPA batches without invoking the target application."""

import argparse
import json
from pathlib import Path

from evals_app.logs import load_trials
from evals_app.models import Plan
from evals_app.validation import validate_execution


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    state = json.loads((args.workspace / "state.json").read_text("utf-8"))
    reports = []
    for experiment in state["experiments"].values():
        if experiment["status"] != "completed":
            continue
        plan = Plan.model_validate(experiment["plan"])
        count = 0
        for key, run in experiment["runs"].items():
            candidate_id, split = key.rsplit(":", 1)
            trials = load_trials(run)
            validate_execution(
                trials,
                [c for c in plan.cases if c.split == split],
                plan.application,
                experiment["candidates"][candidate_id]["config"],
                candidate_id,
                plan.epochs,
                grader=plan.semantic_grader,
            )
            count += len(trials)
        history = experiment.get("gepa", {}).get("history", [])
        for batch in history:
            refs = batch["trial_index"]
            run = {
                "candidate_id": batch["candidate_id"],
                "trial_index": refs,
                "log": refs[0]["log"],
            }
            trials = load_trials(run)
            sample_ids = {ref["sample_id"] for ref in refs}
            cases = [c for c in plan.cases if c.id in sample_ids]
            assert all(c.split in {"train", "selection"} for c in cases)
            validate_execution(
                trials,
                cases,
                plan.application,
                {**plan.application.config, **batch["candidate"]},
                batch["candidate_id"],
                plan.epochs,
                grader=plan.semantic_grader,
            )
            count += len(trials)
        reports.append(
            {
                "experiment_id": experiment["id"],
                "runs": len(experiment["runs"]),
                "gepa_batches": len(history),
                "validated_rounds": count,
            }
        )
    result = {"experiments": reports, "application_calls": 0}
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(text, "utf-8")
    print(text)


if __name__ == "__main__":
    main()
