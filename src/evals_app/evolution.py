"""GEPA text search using partition-limited, isolated Inspect evaluations."""

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
from gepa import optimize
from gepa.core.adapter import EvaluationBatch, GEPAAdapter
from gepa.core.result import GEPAResult

from evals_app.models import Case, Plan, Status, Trial
from evals_app.runtime import Runtime
from evals_app.scoring import resolve, shareable_paths
from evals_app.validation import validate_execution
from evals_app.verification import InsufficientEvidence


class InspectAdapter(GEPAAdapter[Case, dict[str, Any], list[str | None]]):
    def __init__(self, runtime: Runtime, experiment_id: str, plan: Plan) -> None:
        self.runtime, self.experiment_id, self.plan = runtime, experiment_id, plan
        self.allowed = {c.id: c for c in plan.cases if c.split in ("train", "selection")}
        self.history: list[dict[str, Any]] = []

    def evaluate(
        self, batch: list[Case], candidate: dict[str, str], capture_traces: bool = False
    ) -> EvaluationBatch[dict[str, Any], list[str | None]]:
        self.runtime.checkpoint(self.experiment_id)
        self.plan.application.validate_changes(candidate)
        if any(c.id not in self.allowed or c != self.allowed[c.id] for c in batch):
            raise ValueError("GEPA may only read frozen development partitions")
        if len({c.id for c in batch}) != len(batch):
            raise ValueError("Duplicate GEPA batch ids")
        config = {**self.plan.application.config, **candidate}
        from evals_app.models import fingerprint

        candidate_id = f"gepa-{fingerprint(config)[:12]}"
        trials = self.runtime.evaluate(self.experiment_id, self.plan, candidate_id, config, batch)
        if self.plan.semantic_grader:
            grading = self.runtime.store.experiment_fields(self.experiment_id, "ledger")["ledger"][
                "grading"
            ]
            if (
                grading["unknown"]
                or grading["reserved"]
                or grading["spent"] > self.plan.budget.grading + 1e-9
            ):
                raise InsufficientEvidence(
                    "Semantic grading cost requires reconciliation or exceeds the budget"
                )
        try:
            validate_execution(
                trials,
                batch,
                self.plan.application,
                config,
                candidate_id,
                self.plan.epochs,
                grader=self.plan.semantic_grader,
            )
        except ValueError as error:
            raise InsufficientEvidence(str(error)) from error
        outputs, scores, trajectories = [], [], []
        for case in batch:
            rows = sorted([t for t in trials if t.sample_id == case.id], key=lambda t: t.epoch)
            if len(rows) != self.plan.epochs:
                raise InsufficientEvidence("GEPA batch lost an epoch")
            per_epoch = [self.search_score(row) for row in rows]
            outputs.append([r.observation.output if r.observation else None for r in rows])
            scores.append(float(np.mean(per_epoch)))
            trajectories.append(
                {
                    "sample_id": case.id,
                    "input": case.input,
                    "trials": [r.model_dump(mode="json") for r in rows],
                }
            )
        if not all(np.isfinite(scores)) or len(scores) != len(batch):
            raise InsufficientEvidence("GEPA scores must be complete and finite")
        self.history.append(
            {
                "candidate": candidate,
                "candidate_id": candidate_id,
                "scores": scores,
                "trial_index": [
                    {"sample_id": t.sample_id, "epoch": t.epoch, "log": t.log} for t in trials
                ],
            }
        )
        return EvaluationBatch(
            outputs=outputs, scores=scores, trajectories=trajectories if capture_traces else None
        )

    def search_score(self, trial: Trial) -> float:
        for judgment in trial.judgments:
            if judgment.critical and judgment.status not in (Status.PASS, Status.FAIL):
                raise InsufficientEvidence("Critical evidence missing during search")
        applicable = [j for j in trial.judgments if j.status != Status.NOT_APPLICABLE]
        if any(j.status not in (Status.PASS, Status.FAIL) for j in applicable):
            raise InsufficientEvidence("Environment or grading failure blocks GEPA")
        if any(
            j.status == Status.FAIL and j.dimension in self.plan.hard_dimensions for j in applicable
        ):
            return -1e6
        if self.plan.objective == "quality":
            return sum(j.status == Status.PASS for j in applicable) / len(applicable)
        if self.plan.objective == "latency_p95":
            if not trial.attempts:
                raise InsufficientEvidence("Latency missing")
            return -sum(a["latency"] for a in trial.attempts)
        # Search uses additive task cost; terminal cost/success is recomputed independently.
        costs = [a.get("cost") for a in trial.attempts]
        if not costs or any(c is None for c in costs):
            raise InsufficientEvidence("Cost unavailable during search")
        return -sum(float(cost) for cost in costs if cost is not None)

    def make_reflective_dataset(
        self,
        candidate: dict[str, str],
        eval_batch: EvaluationBatch[dict[str, Any], list[str | None]],
        components_to_update: list[str],
    ) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        if eval_batch.trajectories is None:
            raise InsufficientEvidence("Reflection requested without trajectories")
        feedback = []
        analyzed = (
            self.runtime.store.experiment_fields(self.experiment_id, "findings")["findings"]
            if self.runtime is not None and hasattr(self.runtime, "store")
            else []
        )
        for trajectory in eval_batch.trajectories:
            trials = trajectory["trials"]
            # Only share explicitly authorized evidence with an optional external proposer.
            shareable = self.plan.application.evidence_scope == "shareable"
            visible = [
                {
                    "judgments": [
                        {
                            "dimension": j["dimension"],
                            "status": j["status"],
                        }
                        for j in t["judgments"]
                    ],
                    "evidence": self._visible_evidence(t) if shareable else [],
                    "scout_findings": [
                        finding
                        for finding in (analyzed or [])
                        if shareable
                        and finding.get("scope") == "shareable"
                        and finding.get("sample_id") == trajectory["sample_id"]
                        and finding.get("epoch") == t["epoch"]
                        and finding.get("config_fingerprint") == t["config_fingerprint"]
                    ],
                }
                for t in trials
            ]
            feedback.append(
                {
                    "sample_id": trajectory["sample_id"],
                    "feedback": visible,
                    "allowed_values": {
                        k: self.plan.application.assets[k].values for k in components_to_update
                    },
                }
            )
        return {name: feedback for name in components_to_update}

    @staticmethod
    def _visible_evidence(trial: dict[str, Any]) -> list[dict[str, Any]]:
        observation = trial.get("observation")
        if not observation:
            return []
        result = []
        allowed = shareable_paths(
            {path: ref["scope"] for path, ref in observation["evidence"].items()}
        )
        for path, reference in observation["evidence"].items():
            if path not in allowed:
                continue
            try:
                value = resolve(observation["facts"], path)
            except KeyError:
                continue
            result.append({"path": path, "value": value, "reference": reference})
        return result


def search(runtime: Runtime, experiment_id: str, plan: Plan, directory: str) -> dict[str, Any]:
    texts = {name: asset for name, asset in plan.application.assets.items() if asset.kind == "text"}
    if not texts:
        return {"candidates": [], "history": [], "mode": "no_text_assets"}
    adapter = InspectAdapter(runtime, experiment_id, plan)
    counters = {name: 0 for name in texts}

    def propose(
        candidate: dict[str, str],
        reflective_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
        components_to_update: list[str],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, str]:
        runtime.checkpoint(experiment_id)
        if plan.text_proposer:
            response = runtime.reflect(
                experiment_id,
                plan,
                {
                    "candidate": candidate,
                    "feedback": reflective_dataset,
                    "components_to_update": components_to_update,
                    "assets": {
                        k: plan.application.assets[k].model_dump(mode="json")
                        for k in components_to_update
                    },
                    "environment": plan.environment,
                },
            )
            changes = response.get("changes")
            if not isinstance(changes, dict) or set(changes) != set(components_to_update):
                raise ValueError("Proposer returned the wrong text components")
            if any(not isinstance(v, str) for v in changes.values()):
                raise ValueError("Text proposer must return strings")
            plan.application.validate_changes(changes)
            runtime.checkpoint(experiment_id)
            return changes
        reservation = runtime.reserve(experiment_id, "reflection", 0)
        try:
            result = {}
            for name in components_to_update:
                values = [str(v) for v in texts[name].values if str(v) != candidate[name]]
                result[name] = values[counters[name] % len(values)] if values else candidate[name]
                counters[name] += 1
            runtime.checkpoint(experiment_id)
            return result
        finally:
            runtime.settle(experiment_id, reservation, 0)

    result: GEPAResult[list[str | None], int] = optimize(
        seed_candidate={name: str(plan.application.config[name]) for name in texts},
        trainset=[c for c in plan.cases if c.split == "train"],
        valset=[c for c in plan.cases if c.split == "selection"],
        adapter=adapter,
        custom_candidate_proposer=propose,
        skip_perfect_score=False,
        max_metric_calls=plan.search_metric_calls,
        reflection_minibatch_size=2,
        run_dir=directory,
        stop_callbacks=[
            lambda state: (
                runtime.store.experiment_fields(experiment_id, "control")["control"] == "cancel"
            )
        ],
        raise_on_exception=True,
        display_progress_bar=False,
        seed=plan.seed,
        use_cloudpickle=False,
    )
    return {
        "candidates": result.candidates,
        "parents": result.parents,
        "scores": result.val_aggregate_scores,
        "history": adapter.history,
        "mode": "bounded_declared_text_proposer",
        "total_metric_calls": result.total_metric_calls,
    }
