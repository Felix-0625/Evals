"""Shared CLI/HTTP experiment workflow and verified version receipts."""

import math
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import psutil

from evals_app.analysis import findings
from evals_app.build import build_info
from evals_app.evolution import search
from evals_app.logs import load_trials
from evals_app.models import Application, Plan, Proposal, Trial, fingerprint, identifier, now
from evals_app.policies import (
    SearchPolicy,
    configuration_candidates,
    register_policy,
    resolve_policy,
)
from evals_app.runtime import Runtime, Stopped
from evals_app.store import Conflict, Store
from evals_app.verification import compare, require_semantic_coverage, summary

TERMINAL = {"completed", "cancelled", "cancel_pending_confirmation", "blocked", "interrupted"}


class Service:
    def configuration_candidates(self, experiment_id: str, *, key: str) -> Any:
        def update(state: dict[str, Any]) -> Any:
            experiment = state["experiments"][experiment_id]
            if experiment["status"] != "draft" or experiment.get("strategy_study"):
                raise Conflict("Configuration candidates require an unfrozen draft experiment")
            application = Application.model_validate(experiment["plan"]["application"])
            added = configuration_candidates(application, experiment["candidates"])
            experiment.setdefault("operator_interventions", []).append(
                {"kind": "configuration_request", "at": now()}
            )
            return {"added": added}

        return self.store.update(
            update,
            key=f"config-candidates:{experiment_id}:{key}",
            payload={"experiment_id": experiment_id},
        )

    def recover(
        self,
        experiment_id: str,
        expected_fingerprint: str,
        costs: dict[str, float],
        evidence: dict[str, Any],
    ) -> Any:
        """Reconcile every unknown charge after verifying owned and remote processes."""
        if not all(evidence.get(k) for k in ("source", "locator", "collected_at")):
            raise ValueError("Recovery requires verification of worker and remote state")

        def update(state: dict[str, Any]) -> Any:
            experiment = state["experiments"][experiment_id]
            if experiment["status"] not in {
                "blocked",
                "interrupted",
                "cancel_pending_confirmation",
            }:
                raise Conflict("Experiment is not awaiting recovery")
            if experiment["fingerprint"] != expected_fingerprint:
                raise Conflict("Frozen recovery fingerprint differs")
            if experiment.get("build") != self.build:
                raise Conflict(
                    "Execution code changed; create a new experiment instead of resuming"
                )
            worker = experiment.get("worker")
            if worker:
                try:
                    process = psutil.Process(worker["pid"])
                    if process.create_time() == worker["create_time"]:
                        raise Conflict("Owned worker is still alive; stop and verify it first")
                except psutil.NoSuchProcess:
                    pass
            for reservation_id, reservation in experiment["reservations"].items():
                if reservation["status"] != "unknown":
                    continue
                actual = costs.get(reservation_id)
                if actual is None or actual < 0 or not math.isfinite(actual):
                    raise ValueError("Every unknown reservation requires a verified finite cost")
                ledger = experiment["ledger"][reservation["category"]]
                ledger["unknown"] -= 1
                ledger["spent"] += actual
                reservation.update(status="reconciled", actual=actual, evidence=evidence)
            cancelled = experiment["control"] == "cancel"
            experiment.update(
                status="cancelled" if cancelled else "queued",
                control="cancel" if cancelled else "run",
                worker=None,
                recovery_evidence=evidence,
            )
            experiment.setdefault("operator_interventions", []).append(
                {"kind": "recovery", "at": now()}
            )
            return {"id": experiment_id, "status": experiment["status"]}

        result = self.store.update(update)
        self.runtime.wake.set()
        return result

    def reorder(self, experiment_ids: list[str]) -> Any:
        def update(state: dict[str, Any]) -> Any:
            queue = {key for key, e in state["experiments"].items() if e["status"] == "queued"}
            if set(experiment_ids) != queue or len(experiment_ids) != len(queue):
                raise Conflict("Queue ordering must include every queued experiment once")
            for order, key in enumerate(experiment_ids):
                state["experiments"][key]["queue_order"] = order
            return experiment_ids

        return self.store.update(update)

    def __init__(self, directory: Path, *, background: bool = True) -> None:
        self.store = Store(directory)
        self.build = build_info()
        self.runtime = Runtime(self.store)
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self._recover()
        if background:
            self.thread = threading.Thread(target=self._loop, daemon=True, name="evals-owner")
            self.thread.start()

    def _recover(self) -> None:
        def update(state: dict[str, Any]) -> None:
            state.setdefault("calibrations", {})
            for experiment in state["experiments"].values():
                if experiment["status"] not in TERMINAL | {"draft"}:
                    experiment.update(
                        status="interrupted",
                        control="cancel" if experiment["control"] == "cancel" else "pause",
                        reason="Owner restarted; verify worker and remote state before recovery",
                    )
                    for reservation in experiment["reservations"].values():
                        if reservation["status"] == "in_flight":
                            reservation["status"] = "unknown"
                            ledger = experiment["ledger"][reservation["category"]]
                            ledger["reserved"] -= reservation["estimate"]
                            ledger["unknown"] += 1

        self.store.update(update)

    def close(self) -> None:
        self.stopping.set()
        for experiment_id, experiment in self.store.read()["experiments"].items():
            if experiment["status"] not in TERMINAL | {"draft"}:
                self.control(experiment_id, "cancel", operator_request=False)
        self.runtime.wake.set()
        if self.thread:
            self.thread.join(timeout=20)
            if self.thread.is_alive():
                raise RuntimeError("Experiment supervisor did not exit; writer lock retained")
        self.store.close()

    def register(self, application: Application, *, key: str, revision: int | None = None) -> Any:
        def update(state: dict[str, Any]) -> Any:
            if application.id in state["applications"]:
                raise Conflict("Application already registered; use verified version operations")
            record = {
                "declaration": application.model_dump(mode="json"),
                "version": application.version,
                "config": application.config,
                "revision": 0,
                "status": "active",
            }
            state["applications"][application.id] = record
            return record

        return self.store.update(
            update, key=f"register:{key}", payload=application, revision=revision
        )

    def create(
        self,
        plan: Plan,
        proposals: list[Proposal],
        *,
        key: str,
        revision: int | None = None,
        use_gepa: bool | None = None,
        policy: SearchPolicy | None = None,
        observation_of: str | None = None,
        compare_previous: bool = False,
    ) -> Any:
        """Freeze a calibrated plan and declared candidates before any execution."""
        if observation_of and (proposals or use_gepa or policy is not None):
            raise ValueError("Post-adoption observations cannot introduce new search candidates")
        if plan.calibration.artifact == "builtin:rules-v1":
            if plan.application.provenance != "fixture":
                raise ValueError("Real applications require registered business-rubric calibration")
        else:
            record = self.store.read()["calibrations"].get(plan.calibration.artifact)
            if (
                not record
                or record["application_id"] != plan.application.id
                or record["calibration"] != plan.calibration.model_dump(mode="json")
            ):
                raise ValueError(
                    "Business scorer calibration does not match the registered artifact"
                )
            required_rules = {
                fingerprint(rule)
                for case in plan.cases
                for rule in case.rules
                if rule.operator != "semantic"
            }
            if not required_rules.issubset(record.get("rule_fingerprints", [])):
                raise ValueError("The frozen plan contains business rules absent from calibration")
        self._check_calibration()

        def update(state: dict[str, Any]) -> Any:
            app = state["applications"][plan.application.id]
            if app.get("status", "active") != "active":
                raise Conflict("Application effective state requires reconciliation")
            if (
                app["version"] != plan.application.version
                or app["config"] != plan.application.config
                or app["declaration"] != plan.application.model_dump(mode="json")
            ):
                raise Conflict("Frozen application declaration or baseline is stale")
            from evals_app.monitoring import observation_binding, regression_binding

            binding = observation_binding(state, observation_of, plan, compare_previous)
            reuse = regression_binding(state, plan)
            search_policy = resolve_policy(state, policy, use_gepa, plan.application.id)
            register_policy(state, search_policy)
            candidates = {
                "baseline": {
                    "id": "baseline",
                    "config": plan.application.config,
                    "source": "baseline",
                    "hypothesis": "Original application",
                    "risk": "Existing behavior",
                    "parent_id": None,
                }
            }
            for proposal in proposals:
                self._validate_proposal(plan, proposal)
                candidate_id = identifier("candidate")
                candidates[candidate_id] = {
                    **proposal.model_dump(mode="json"),
                    "id": candidate_id,
                    "config": {**plan.application.config, **proposal.changes},
                }
            experiment_id = identifier("experiment")
            if search_policy.configuration_candidates and not observation_of:
                configuration_candidates(plan.application, candidates)
            if binding.get("observation_control"):
                candidates["previous_config"] = {
                    "id": "previous_config",
                    "config": binding["observation_control"]["config"],
                    "source": "adoption_control",
                    "hypothesis": "Previous configuration on paired new tasks",
                    "risk": "Same effective code version; isolates configuration effects only",
                    "parent_id": "baseline",
                }
            record = {
                "id": experiment_id,
                "application_id": plan.application.id,
                "plan": plan.model_dump(mode="json"),
                "fingerprint": fingerprint(plan),
                "build": self.build,
                "status": "draft",
                "control": "run",
                "stage": "prepared",
                "created_at": now(),
                "use_gepa": search_policy.use_gepa,
                "policy": search_policy.model_dump(mode="json"),
                "operator_interventions": [
                    {"kind": "manual_proposal", "candidate_id": c["id"], "at": now()}
                    for c in candidates.values()
                    if c["source"] == "manual"
                ],
                "candidates": candidates,
                "runs": {},
                "findings": [],
                "reports": {},
                "worker": None,
                "ledger": {
                    c: {"spent": 0, "reserved": 0, "unknown": 0}
                    for c in ("application", "grading", "analysis", "reflection")
                },
                "reservations": {},
                "queue_order": len(state["experiments"]),
                **binding,
                **reuse,
            }
            state["experiments"][experiment_id] = record
            return {"id": experiment_id, "fingerprint": record["fingerprint"]}

        return self.store.update(
            update,
            key=f"create:{key}",
            payload={
                "plan": plan.model_dump(mode="json"),
                "proposals": [p.model_dump(mode="json") for p in proposals],
                "use_gepa": use_gepa,
                **({"policy": policy.model_dump(mode="json")} if policy is not None else {}),
                **(
                    {"observation_of": observation_of, "compare_previous": compare_previous}
                    if observation_of is not None
                    else {}
                ),
            },
            revision=revision,
        )

    @staticmethod
    def _check_calibration() -> None:
        from evals_app.calibration import calibrate

        result = calibrate()
        if result["correct"] != result["expected"]:
            raise ValueError("Deterministic scorer calibration failed")

    @staticmethod
    def _validate_proposal(plan: Plan, proposal: Proposal) -> None:
        if proposal.expected_baseline != plan.application.version:
            raise Conflict("Proposal baseline is stale")
        if proposal.parent_id != "baseline":
            raise ValueError("External proposals must target the frozen baseline")
        plan.application.validate_changes(proposal.changes)
        if not proposal.changes or all(
            plan.application.config[k] == v for k, v in proposal.changes.items()
        ):
            raise ValueError("Proposal has no change")

    def submit(self, experiment_id: str, proposal: Proposal, *, key: str) -> Any:
        def update(state: dict[str, Any]) -> Any:
            experiment = state["experiments"][experiment_id]
            if experiment["status"] != "draft":
                raise Conflict("Candidates freeze when the experiment is queued")
            if experiment.get("strategy_study"):
                raise Conflict("Strategy study candidates are already frozen")
            self._validate_proposal(Plan.model_validate(experiment["plan"]), proposal)
            candidate_id = identifier("candidate")
            experiment["candidates"][candidate_id] = {
                **proposal.model_dump(mode="json"),
                "id": candidate_id,
                "config": {**experiment["plan"]["application"]["config"], **proposal.changes},
            }
            if proposal.source == "manual":
                experiment.setdefault("operator_interventions", []).append(
                    {"kind": "manual_proposal", "candidate_id": candidate_id, "at": now()}
                )
            return {"id": candidate_id}

        return self.store.update(update, key=f"proposal:{experiment_id}:{key}", payload=proposal)

    def start(self, experiment_id: str, expected_fingerprint: str) -> Any:
        """Queue only the exact frozen plan; stale client fingerprints are rejected."""

        def update(state: dict[str, Any]) -> Any:
            experiment = state["experiments"][experiment_id]
            if experiment["fingerprint"] != expected_fingerprint:
                raise Conflict("Resume fingerprint does not match the frozen experiment")
            if experiment.get("build") != self.build:
                raise Conflict("Execution code changed since the experiment was prepared")
            if experiment["status"] != "draft":
                raise Conflict(
                    "Only prepared experiments can start; interrupted work needs reconciliation"
                )
            from evals_app.monitoring import regression_binding
            from evals_app.semantic import check_calibration

            plan = Plan.model_validate(experiment["plan"])
            check_calibration(state, plan, self.build)
            regression_binding(state, plan)
            experiment.update(status="queued", control="run")
            return {"id": experiment_id, "status": "queued"}

        result = self.store.update(update)
        self.runtime.wake.set()
        return result

    def control(self, experiment_id: str, action: str, *, operator_request: bool = True) -> Any:
        """Record experiment intent separately from the native task control result."""
        if action not in {"pause", "resume", "cancel"}:
            raise ValueError("Unknown experiment command")

        def update(state: dict[str, Any]) -> Any:
            experiment = state["experiments"][experiment_id]
            if experiment["status"] in TERMINAL:
                raise Conflict("Terminal experiments cannot dispatch more work")
            if operator_request:
                experiment.setdefault("operator_interventions", []).append(
                    {"kind": action, "at": now()}
                )
            experiment["control"] = "run" if action == "resume" else action
            if action == "cancel":
                experiment["status"] = "cancel_pending"
                if (
                    experiment_id not in self.runtime.processes
                    and experiment["stage"] == "prepared"
                ):
                    experiment["status"] = "cancelled"
                    from evals_app.semantic import fail_calibration

                    fail_calibration(state, experiment, "cancelled", "Cancelled before execution")
            elif action == "pause":
                experiment["status"] = "pause_pending"
            else:
                experiment["status"] = "running" if experiment["stage"] != "prepared" else "queued"
            return {"id": experiment_id, "status": experiment["status"]}

        result = self.store.update(update)
        if experiment_id in self.runtime.processes and action != "cancel":
            try:
                result["native"] = self.runtime.native(experiment_id, action)
            except (RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
                result["native_unavailable"] = str(error)
        self.runtime.wake.set()
        return result

    def _loop(self) -> None:
        while not self.stopping.is_set():
            experiment_id = self.store.next_queued()
            if experiment_id:
                self.run(experiment_id)
            else:
                self.runtime.wake.wait(0.2)
                self.runtime.wake.clear()

    def _phase(self, experiment_id: str, stage: str) -> None:
        self.runtime.checkpoint(experiment_id)
        self.runtime.patch(experiment_id, stage=stage, status="running")

    def _evaluate(
        self, experiment_id: str, plan: Plan, candidate: dict[str, Any], split: str
    ) -> list[Trial]:
        self.runtime.checkpoint(experiment_id)
        key = f"{candidate['id']}:{split}"
        existing = self.store.read()["experiments"][experiment_id]["runs"].get(key)
        if existing:
            self.runtime.verify_remote_write_state(plan)
            self._check_grading_cost(experiment_id, plan)
            return load_trials(existing)
        cases = [c for c in plan.cases if c.split == split]
        trials = self.runtime.evaluate(
            experiment_id,
            plan,
            candidate["id"],
            candidate["config"],
            cases,
            scan=split in ("train", "selection"),
        )

        def update(state: dict[str, Any]) -> None:
            state["experiments"][experiment_id]["runs"][key] = {
                "candidate_id": candidate["id"],
                "log": trials[0].log,
                "trial_index": [
                    {
                        "sample_id": t.sample_id,
                        "epoch": t.epoch,
                        "candidate_id": t.candidate_id,
                        "log": t.log,
                    }
                    for t in trials
                ],
                "summary": summary(trials),
            }

        self.store.update(update)
        self.runtime.verify_remote_write_state(plan)
        self._check_grading_cost(experiment_id, plan)
        return trials

    def _check_grading_cost(self, experiment_id: str, plan: Plan) -> None:
        if plan.semantic_grader:
            grading = self.store.experiment_fields(experiment_id, "ledger")["ledger"]["grading"]
            if (
                grading["unknown"]
                or grading["reserved"]
                or grading["spent"] > plan.budget.grading + 1e-9
            ):
                raise ValueError(
                    "Semantic grading cost requires reconciliation or exceeds the budget"
                )

    def experiment(self, experiment_id: str) -> dict[str, Any]:
        experiment = self.store.read()["experiments"][experiment_id]
        for run in experiment["runs"].values():
            run["trials"] = [trial.model_dump(mode="json") for trial in load_trials(run)]
        return experiment

    def run(self, experiment_id: str) -> None:
        """Evaluate development partitions first; only selected candidates see final data."""
        experiment = self.store.read()["experiments"][experiment_id]
        plan = Plan.model_validate(experiment["plan"])
        try:
            from evals_app.semantic import analyze_trials, check_calibration, run_calibration

            if experiment.get("pending_semantic_calibration"):
                run_calibration(self, experiment_id, plan)
                return
            check_calibration(self.store.read(), plan, self.build)
            if experiment.get("observation_of"):
                from evals_app.monitoring import run_observation

                run_observation(self, experiment_id, plan)
                return
            self._phase(experiment_id, "baseline")
            baseline = experiment["candidates"]["baseline"]
            train = self._evaluate(experiment_id, plan, baseline, "train")
            selection = self._evaluate(experiment_id, plan, baseline, "selection")
            require_semantic_coverage(plan, train)
            require_semantic_coverage(plan, selection)
            self._phase(experiment_id, "analysis")
            reservation = self.runtime.reserve(experiment_id, "analysis", 0)
            start = time.monotonic()
            detected = [
                {
                    **finding,
                    "sample_id": t.sample_id,
                    "epoch": t.epoch,
                    "log": t.log,
                    "analyzer_version": plan.analyzer_version,
                    "candidate_id": t.candidate_id,
                    "config_fingerprint": t.config_fingerprint,
                }
                for t in train
                for finding in findings(
                    t.observation.model_dump(mode="json") if t.observation else None
                )
            ]
            self.runtime.patch(experiment_id, findings=detected)
            self.runtime.settle(experiment_id, reservation, 0, duration=time.monotonic() - start)
            if plan.semantic_analyzer is not None:
                self._phase(experiment_id, "semantic_analysis")
                detected += analyze_trials(self, experiment_id, plan, train)
                self.runtime.patch(experiment_id, findings=detected)
            self._phase(experiment_id, "candidates")
            if experiment["use_gepa"] and "gepa" not in experiment:
                directory = self.store.directory / "gepa" / experiment_id
                directory.mkdir(parents=True, exist_ok=True)
                result = search(self.runtime, experiment_id, plan, str(directory))

                def add_candidates(state: dict[str, Any]) -> None:
                    record = state["experiments"][experiment_id]
                    record["gepa"] = result
                    texts = result["candidates"] + [h["candidate"] for h in result["history"]]
                    for text in texts:
                        config = {**plan.application.config, **text}
                        if config == plan.application.config:
                            continue
                        candidate_id = f"gepa-{fingerprint(config)[:12]}"
                        record["candidates"][candidate_id] = {
                            "id": candidate_id,
                            "config": config,
                            "changes": text,
                            "source": "gepa",
                            "parent_id": "baseline",
                            "hypothesis": "GEPA bounded text proposal",
                            "risk": "Prompt rule regression",
                        }

                self.store.update(add_candidates)
            self._phase(experiment_id, "selection")
            candidates = self.store.read()["experiments"][experiment_id]["candidates"]
            reports = {}
            for candidate_id, candidate in candidates.items():
                if candidate_id == "baseline":
                    continue
                trials = self._evaluate(experiment_id, plan, candidate, "selection")
                reports[candidate_id] = compare(plan, selection, trials, phase="selection")
                self.runtime.patch(experiment_id, selection_reports=reports)
            eligible = [c for c, r in reports.items() if r["decision"] == "recommend"]
            selected = max(eligible, key=lambda c: reports[c]["gain"]) if eligible else None
            self.runtime.patch(experiment_id, selected_candidate=selected)
            if selected:
                self._phase(experiment_id, "independent_verification")
                final_reports = []
                for split in ("holdout", "counterexample"):
                    original = self._evaluate(experiment_id, plan, baseline, split)
                    changed = self._evaluate(experiment_id, plan, candidates[selected], split)
                    final_reports.append(compare(plan, original, changed, phase=split))
                decision = (
                    "reject"
                    if any(r["decision"] == "reject" for r in final_reports)
                    else "recommend"
                    if all(r["decision"] == "recommend" for r in final_reports)
                    else "insufficient_evidence"
                )
                report = {
                    "candidate_id": selected,
                    "decision": decision,
                    "partitions": final_reports,
                    "baseline": plan.application.version,
                    "fingerprint": fingerprint(plan),
                    "provenance": plan.application.provenance,
                    "created_at": now(),
                }
                self.runtime.patch(experiment_id, reports={selected: report})
            self.runtime.checkpoint(experiment_id)
            self.runtime.patch(
                experiment_id,
                stage="finished",
                status="completed",
                conclusion="independent_report_available" if selected else "keep_baseline",
                reason=None,
            )
            from evals_app.monitoring import record_observation

            record_observation(self, experiment_id)
        except Stopped as error:
            if experiment.get("pending_semantic_calibration"):
                from evals_app.semantic import fail_calibration

                message = str(error)
                self.store.update(
                    lambda state: fail_calibration(state, experiment, "cancelled", message)
                )
            uncertain = plan.application.adapter == "http" or not plan.application.read_only
            self.runtime.patch(
                experiment_id,
                status="cancel_pending_confirmation" if uncertain else "cancelled",
                reason=str(error),
            )
        except Exception as error:
            if experiment.get("pending_semantic_calibration"):
                from evals_app.semantic import fail_calibration

                message = str(error)
                self.store.update(
                    lambda state: fail_calibration(state, experiment, "failed", message)
                )
            self.runtime.patch(
                experiment_id, status="blocked", reason=f"{type(error).__name__}: {error}"
            )

    def operation(
        self,
        experiment_id: str,
        candidate_id: str,
        expected_baseline: str,
        requested_version: str,
        *,
        key: str,
        rollback: str | None = None,
    ) -> Any:
        """Prepare an adoption request after independent verification."""
        from evals_app.adoption import operation

        return operation(
            self,
            experiment_id,
            candidate_id,
            expected_baseline,
            requested_version,
            key=key,
            rollback=rollback,
        )

    def receipt(
        self,
        operation_id: str,
        *,
        executor: str,
        actual_version: str,
        config_fingerprint: str,
        status: str,
        evidence: dict[str, Any],
        key: str,
    ) -> Any:
        """Record verified effective state without executing application changes."""
        from evals_app.adoption import receipt

        return receipt(
            self,
            operation_id,
            executor=executor,
            actual_version=actual_version,
            config_fingerprint=config_fingerprint,
            status=status,
            evidence=evidence,
            key=key,
        )
