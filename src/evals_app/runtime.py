"""Supervised Inspect subprocesses, durable controls and cost reservations."""

import json
import math
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Literal

import httpx
import psutil
from inspect_ai._control.discovery import list_discovered_servers
from inspect_ai._control.transport import control_connection

from evals_app.coordination import atomic_json
from evals_app.models import Case, Plan, Trial, fingerprint, identifier, now
from evals_app.store import Store
from evals_app.validation import validate_execution


class Stopped(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class Runtime:
    def analyze(
        self,
        experiment_id: str,
        plan: Plan,
        rows: list[dict[str, Any]],
        *,
        role: Literal["analysis", "grading"] = "analysis",
    ) -> dict[str, Any]:
        definition = plan.semantic_analyzer if role == "analysis" else plan.semantic_grader
        if definition is None:
            raise ValueError("No frozen semantic analyzer")
        self.checkpoint(experiment_id)
        directory = self.store.directory / "semantic" / experiment_id / identifier(role)
        directory.mkdir(parents=True)
        coordination = directory / "calls"
        coordination.mkdir()
        path = directory / "job.json"
        job = {
            "definition": definition.model_dump(mode="json"),
            "role": role,
            "rows": rows,
            "coordination_dir": str(coordination),
            "partial_path": str(path.with_suffix(".partial.jsonl")),
            "build": self.store.experiment_fields(experiment_id, "build")["build"],
        }
        path.write_text(json.dumps(job, ensure_ascii=False), "utf-8")
        self.store.update(
            lambda state: (
                state["experiments"][experiment_id]
                .setdefault("semantic_jobs", [])
                .append(str(path))
            )
        )
        outstanding: set[str] = set()
        process = None
        started = time.monotonic()
        try:
            with (directory / "worker.txt").open("wb") as output:
                process = subprocess.Popen(
                    [sys.executable, "-m", "evals_app.semantic_worker", str(path)],
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
                self.processes[experiment_id] = process
                self.patch(
                    experiment_id,
                    status="running",
                    worker={
                        "pid": process.pid,
                        "create_time": psutil.Process(process.pid).create_time(),
                        "kind": "semantic",
                        "job_file": str(path.resolve()),
                        "started_at": now(),
                    },
                    native_control={"status": "not_an_eval"},
                )
                while process.poll() is None:
                    self.service_calls(
                        experiment_id,
                        plan,
                        coordination,
                        outstanding,
                        category=role,
                        estimate=definition.estimate_per_call,
                    )
                    control = self.store.experiment_fields(experiment_id, "control")["control"]
                    if control == "cancel":
                        self.reap(process)
                        raise Stopped("Semantic worker cancelled and reaped")
                    if (
                        control != "pause"
                        and time.monotonic() - started
                        > definition.timeout * max(1, len(rows)) * 3 + 90
                    ):
                        self.reap(process)
                        raise TimeoutError("Semantic analysis exceeded its frozen timeout")
                    self.wake.wait(0.1)
                    self.wake.clear()
                result_path = path.with_suffix(".result.json")
                if not result_path.exists():
                    raise RuntimeError(
                        f"Semantic worker exited without results; see {directory / 'worker.txt'}"
                    )
                result = json.loads(result_path.read_text("utf-8"))
                if process.returncode or "error" in result:
                    raise RuntimeError(f"Semantic analysis failed: {result}")
                if [r["id"] for r in result["rows"]] != [r["id"] for r in rows]:
                    raise ValueError("Semantic worker lost or reordered requested transcripts")
                ledger = self.store.experiment_fields(experiment_id, "ledger")["ledger"][role]
                if ledger["unknown"] or ledger["reserved"]:
                    raise ValueError("Semantic costs require reconciliation before selection")
                if ledger["spent"] > getattr(plan.budget, role) + 1e-9:
                    raise BudgetExceeded(f"Actual semantic {role} cost exceeds the frozen budget")
                self.checkpoint(experiment_id)
                return {**result, "artifact": str(result_path)}
        finally:
            if process is not None:
                self.reap(process)
            self.processes.pop(experiment_id, None)
            self.patch(experiment_id, worker=None)
            for reservation_id in outstanding:
                self.settle(
                    experiment_id, reservation_id, None, duration=time.monotonic() - started
                )

    def reflect(self, experiment_id: str, plan: Plan, payload: dict[str, Any]) -> dict[str, Any]:
        self.checkpoint(experiment_id)
        reservation = self.reserve(experiment_id, "reflection", plan.reflection_estimate)
        directory = self.store.directory / "reflections" / experiment_id / identifier("reflection")
        directory.mkdir(parents=True)
        path = directory / "job.json"
        path.write_text(json.dumps({"entrypoint": plan.text_proposer, "payload": payload}), "utf-8")
        start = time.monotonic()
        actual = None
        process = None
        try:
            with (directory / "worker.txt").open("wb") as output:
                process = subprocess.Popen(
                    [sys.executable, "-m", "evals_app.reflection", str(path)],
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
                self.processes[experiment_id] = process
                self.patch(
                    experiment_id,
                    stage="reflection",
                    worker={
                        "pid": process.pid,
                        "create_time": psutil.Process(process.pid).create_time(),
                        "kind": "reflection",
                        "started_at": now(),
                        "job": str(path),
                    },
                    native_control={"status": "not_an_eval"},
                )
                while process.poll() is None:
                    experiment = self.store.experiment_fields(experiment_id, "control")
                    if experiment["control"] == "cancel":
                        self.reap(process)
                        raise Stopped("Reflection process cancelled and reaped")
                    if time.monotonic() - start > plan.application.timeout + 30:
                        self.reap(process)
                        raise TimeoutError("Reflection exceeded its timeout")
                    self.wake.wait(0.1)
                    self.wake.clear()
                result = json.loads(path.with_suffix(".result.json").read_text("utf-8"))
                if process.returncode or "error" in result:
                    raise RuntimeError(f"Reflection failed: {result}")
                value = result.get("cost")
                if value is not None:
                    import math

                    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                        raise ValueError("Invalid reflection cost")
                    if not result.get("cost_source") or not result.get("pricing_version"):
                        raise ValueError("Reflection cost requires source and tariff version")
                    actual = float(value)
                if actual is None:
                    raise ValueError(
                        "Reflection cost is unknown; reconcile before another candidate"
                    )
                self.checkpoint(experiment_id)
                return result
        finally:
            if process:
                self.reap(process)
            self.processes.pop(experiment_id, None)
            self.patch(experiment_id, worker=None)
            self.settle(experiment_id, reservation, actual, duration=time.monotonic() - start)

    def __init__(self, store: Store) -> None:
        self.store = store
        self.wake = threading.Event()
        self.processes: dict[str, subprocess.Popen[bytes]] = {}

    def patch(self, experiment_id: str, **fields: Any) -> None:
        def update(state: dict[str, Any]) -> None:
            state["experiments"][experiment_id].update(fields, updated_at=now())

        self.store.update(update)

    def checkpoint(self, experiment_id: str) -> None:
        """Honor pause and cancellation before dispatching another unit of work."""
        while True:
            experiment = self.store.experiment_fields(experiment_id, "control")
            if experiment["control"] == "cancel":
                raise Stopped("Experiment cancellation requested")
            if experiment["control"] != "pause":
                return
            self.patch(experiment_id, status="paused")
            self.wake.wait(0.2)
            self.wake.clear()

    def reserve(
        self,
        experiment_id: str,
        category: str,
        amount: float,
        *,
        wait: bool = True,
        provenance: dict[str, Any] | None = None,
    ) -> str:
        """Reserve estimated spend atomically, including shared strategy-arm budgets."""
        if not math.isfinite(amount) or amount < 0:
            raise ValueError("Reservation must be finite and nonnegative")
        if wait:
            self.checkpoint(experiment_id)
        reservation_id = identifier("reservation")

        def update(state: dict[str, Any]) -> None:
            experiment = state["experiments"][experiment_id]
            if experiment["control"] != "run":
                raise Stopped("Control changed before dispatch")
            ledger = experiment["ledger"][category]
            if ledger["unknown"]:
                raise BudgetExceeded("Previous call has unknown cost; reconciliation required")
            if (
                ledger["spent"] + ledger["reserved"] + amount
                > experiment["plan"]["budget"][category] + 1e-9
            ):
                raise BudgetExceeded(f"{category} budget exhausted")
            if study_id := experiment.get("strategy_study"):
                study = state["strategies"][study_id]
                arm = next((ids for ids in study["arms"].values() if experiment_id in ids), None)
                if arm is None:
                    raise BudgetExceeded("Experiment has no frozen strategy arm")
                arm_ledgers = [
                    item
                    for member in arm
                    for item in state["experiments"][member]["ledger"].values()
                ]
                if any(item["unknown"] for item in arm_ledgers):
                    raise BudgetExceeded("Strategy arm has unknown costs; reconcile them first")
                committed = math.fsum(item["spent"] + item["reserved"] for item in arm_ledgers)
                if committed + amount > study["budget_per_arm"] + 1e-9:
                    raise BudgetExceeded("Strategy arm budget exhausted")
            ledger["reserved"] += amount
            experiment["reservations"][reservation_id] = {
                "category": category,
                "estimate": amount,
                "status": "in_flight",
                "created_at": now(),
                "provenance": provenance,
            }

        self.store.update(update)
        return reservation_id

    def settle(
        self,
        experiment_id: str,
        reservation_id: str,
        actual: float | None,
        *,
        duration: float = 0,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """Record actual spend once; an unknown charge blocks subsequent dispatch."""
        if actual is not None and (not math.isfinite(actual) or actual < 0):
            raise ValueError("Actual cost must be finite and nonnegative")

        def update(state: dict[str, Any]) -> None:
            experiment = state["experiments"][experiment_id]
            reservation = experiment["reservations"][reservation_id]
            ledger = experiment["ledger"][reservation["category"]]
            if reservation["status"] != "in_flight":
                raise ValueError("Reservation is already settled")
            ledger["reserved"] -= reservation["estimate"]
            if actual is None:
                ledger["unknown"] += 1
            else:
                ledger["spent"] += actual
            reservation.update(
                status="unknown" if actual is None else "settled",
                actual=actual,
                duration=duration,
                settled_at=now(),
                usage=usage,
            )

        self.store.update(update)

    def service_calls(
        self,
        experiment_id: str,
        plan: Plan,
        directory: Path,
        outstanding: set[str],
        *,
        category: str = "application",
        estimate: float | None = None,
    ) -> None:
        for path in sorted(directory.glob("*.request.json")):
            reply = path.with_name(path.name.replace(".request.json", ".reply.json"))
            if reply.exists():
                continue
            request = json.loads(path.read_text("utf-8"))
            result: dict[str, Any]
            try:
                if request["action"] == "reserve":
                    control = self.store.experiment_fields(experiment_id, "control")["control"]
                    if control == "pause":
                        if not outstanding:
                            self.patch(experiment_id, status="paused")
                        continue
                    reservation_id = self.reserve(
                        experiment_id,
                        category,
                        plan.application.estimate_per_attempt if estimate is None else estimate,
                        wait=False,
                        provenance={
                            "job": directory.parent.name,
                            "attempt_id": request["attempt_id"],
                            "sample_id": request["sample_id"],
                            "epoch": request["epoch"],
                        },
                    )
                    outstanding.add(reservation_id)
                    result = {"reservation_id": reservation_id}
                elif request["action"] == "settle":
                    reservation_id = request["reservation_id"]
                    if reservation_id not in outstanding:
                        raise ValueError("Reservation does not belong to this active job")
                    self.settle(
                        experiment_id,
                        reservation_id,
                        request["actual"],
                        duration=request["duration"],
                        usage=request.get("usage"),
                    )
                    outstanding.remove(reservation_id)
                    result = {"settled": True}
                else:
                    raise ValueError("Unsupported worker coordination action")
            except Stopped as error:
                if self.store.experiment_fields(experiment_id, "control")["control"] == "pause":
                    continue
                result = {"error": str(error)}
            except (BudgetExceeded, ValueError) as error:
                result = {"error": str(error)}
            atomic_json(reply, result)

    def discover(self, experiment_id: str) -> Any:
        process = self.processes.get(experiment_id)
        if process is None or process.poll() is not None:
            return None
        expected = self.store.experiment_fields(experiment_id, "worker")["worker"] or {}
        try:
            actual_start = psutil.Process(process.pid).create_time()
        except psutil.Error:
            return None
        if actual_start != expected.get("create_time"):
            return None
        job_file = expected.get("job_file")
        if not job_file:
            return None
        try:
            identity = json.loads(Path(job_file).with_suffix(".identity.json").read_text("utf-8"))
            runtime_process = psutil.Process(identity["pid"])
            owned_pids = {
                process.pid,
                *(child.pid for child in psutil.Process(process.pid).children(recursive=True)),
            }
            if (
                identity["pid"] not in owned_pids
                or runtime_process.create_time() != identity["create_time"]
                or identity["job_file"] != str(Path(job_file).resolve())
            ):
                return None
        except (OSError, ValueError, KeyError, psutil.Error):
            return None
        for server in list_discovered_servers():
            if server.pid == identity["pid"] and server.started_at >= identity["create_time"] - 2:
                self.patch(
                    experiment_id,
                    native_control={
                        "pid": server.pid,
                        "socket": str(server.socket_path),
                        "api_version": server.api_version,
                        "run_id": server.run_id,
                        "status": "available",
                    },
                )
                return server
        return None

    def native(
        self,
        experiment_id: str,
        action: str,
        *,
        sample_id: str | None = None,
        epoch: int | None = None,
    ) -> Any:
        self.store.experiment_fields(experiment_id, "control")
        if action not in {"tasks", "pause", "resume", "cancel", "requeue"}:
            raise ValueError("Unsupported native control command")
        server = self.discover(experiment_id)
        if server is None:
            raise RuntimeError("Inspect control service unavailable for the owned worker")
        connection = control_connection(server.socket_path)
        transport = httpx.HTTPTransport(uds=connection.uds)
        with httpx.Client(
            transport=transport,
            base_url=connection.base_url,
            headers=connection.headers,
            timeout=3,
            trust_env=False,
        ) as client:
            tasks = client.get("/tasks")
            tasks.raise_for_status()
            if action == "tasks":
                return tasks.json()
            if action in {"pause", "resume"}:
                result = client.post(f"/{action}")
                result.raise_for_status()
                return result.json()
            if action == "requeue":
                if sample_id is None or epoch is None or epoch < 1:
                    raise ValueError("Sample requeue requires an explicit sample and epoch")
                worker = self.store.experiment_fields(experiment_id, "worker")["worker"]
                job = json.loads(Path(worker["job_file"]).read_text("utf-8"))
                if (
                    sample_id not in {c["id"] for c in job["cases"]}
                    or epoch > job["plan"]["epochs"]
                ):
                    raise ValueError("Sample is not part of the owned active job")
                if self.store.experiment_fields(experiment_id, "control")["control"] != "run":
                    raise ValueError("Sample requeue requires a running experiment")
                rows = tasks.json()
                if len(rows) != 1 or rows[0]["run_id"] != server.run_id:
                    raise ValueError("Native task identity does not match the owned run")
                result = client.post(
                    f"/evals/{rows[0]['eval_id']}/sample/requeue",
                    params={"sample_id": sample_id, "epoch": epoch},
                )
                result.raise_for_status()
                return result.json()
            rows = tasks.json()
            if isinstance(rows, dict):
                rows = rows.get("tasks", [])
            results = []
            for row in rows:
                result = client.post(f"/tasks/{row['task_id']}/cancel")
                result.raise_for_status()
                results.append(result.json())
            return results

    @staticmethod
    def reap(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            try:
                children = psutil.Process(process.pid).children(recursive=True)
            except psutil.Error:
                children = []
            for child in reversed(children):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            process.kill()
        process.wait(timeout=10)

    def verify_remote_write_state(self, plan: Plan) -> None:
        application = plan.application
        if application.adapter != "http" or application.read_only:
            return
        for experiment in self.store.read()["experiments"].values():
            if experiment["application_id"] == application.id and (
                experiment["ledger"]["application"]["unknown"]
                or experiment["status"] == "cancel_pending_confirmation"
            ):
                raise ValueError(
                    "Remote write requires state and cost verification before another dispatch"
                )

    def evaluate(
        self,
        experiment_id: str,
        plan: Plan,
        candidate_id: str,
        config: dict[str, Any],
        cases: list[Case],
        *,
        scan: bool = False,
    ) -> list[Trial]:
        self.checkpoint(experiment_id)
        self.verify_remote_write_state(plan)
        job_id = identifier("job")
        # Candidate identity remains in metadata; compact paths fit Windows rename limits.
        directory = self.store.directory / "logs" / experiment_id / job_id
        directory.mkdir(parents=True)
        coordination = directory / "calls"
        coordination.mkdir()
        outstanding: set[str] = set()
        grader = (
            plan.semantic_grader
            if any(rule.operator == "semantic" for case in cases for rule in case.rules)
            else None
        )
        grading_coordination = directory / "grade"
        grading_outstanding: set[str] = set()
        if grader:
            grading_coordination.mkdir()
        path = directory / "job.json"
        # The worker sees only the selected partition's contents, even during search.
        execution_plan = {
            "application": plan.application.model_dump(mode="json"),
            "epochs": plan.epochs,
            "platform_retries": plan.platform_retries,
            "retryable": plan.retryable,
            "semantic_grader": grader.model_dump(mode="json") if grader else None,
        }
        job = {
            "plan": execution_plan,
            "cases": [c.model_dump(mode="json") for c in cases],
            "experiment_id": experiment_id,
            "candidate_id": candidate_id,
            "config": config,
            "log_dir": str(directory),
            "scan": scan,
            "plan_fingerprint": fingerprint(plan),
            "build": self.store.experiment_fields(experiment_id, "build")["build"],
            "coordination_dir": str(coordination),
            "grading_dir": str(grading_coordination) if grader else None,
        }
        path.write_text(json.dumps(job, ensure_ascii=False), "utf-8")
        started = time.monotonic()
        process = None
        try:
            with (directory / "worker.txt").open("wb") as output:
                process = subprocess.Popen(
                    [sys.executable, "-m", "evals_app.worker", str(path)],
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
                self.processes[experiment_id] = process
                self.patch(
                    experiment_id,
                    status="running",
                    worker={
                        "pid": process.pid,
                        "create_time": psutil.Process(process.pid).create_time(),
                        "job": job_id,
                        "job_file": str(path.resolve()),
                        "candidate_id": candidate_id,
                        "started_at": now(),
                    },
                    native_control={"status": "starting"},
                )
                timeout = (
                    plan.application.timeout
                    * len(cases)
                    * plan.epochs
                    * (plan.platform_retries + 1)
                    + 90
                )
                if grader:
                    timeout += grader.timeout * len(cases) * plan.epochs * 3
                while process.poll() is None:
                    self.service_calls(experiment_id, plan, coordination, outstanding)
                    if grader:
                        self.service_calls(
                            experiment_id,
                            plan,
                            grading_coordination,
                            grading_outstanding,
                            category="grading",
                            estimate=grader.estimate_per_call,
                        )
                    experiment = self.store.experiment_fields(experiment_id, "control")
                    if experiment["control"] == "cancel":
                        try:
                            result = self.native(experiment_id, "cancel")
                            self.patch(experiment_id, native_cancel=result)
                        except (RuntimeError, httpx.HTTPError, KeyError, OSError) as error:
                            self.patch(experiment_id, native_cancel_unavailable=str(error))
                        self.reap(process)
                        raise Stopped("Active worker cancelled and reaped")
                    if time.monotonic() - started > timeout and experiment["control"] != "pause":
                        self.reap(process)
                        raise TimeoutError("Inspect worker exceeded its execution timeout")
                    self.wake.wait(0.1)
                    self.wake.clear()
                result_path = path.with_suffix(".result.json")
                if not result_path.exists():
                    raise RuntimeError(
                        f"Worker exited without a result; see {directory / 'worker.txt'}"
                    )
                result = json.loads(result_path.read_text("utf-8"))
                if process.returncode or "error" in result:
                    raise RuntimeError(f"Inspect worker failed: {result}")
                trials = [Trial.model_validate(t) for t in result["trials"]]
                validate_execution(
                    trials,
                    cases,
                    plan.application,
                    config,
                    candidate_id,
                    plan.epochs,
                    run_id=result["run_id"],
                    log=result["log"],
                    grader=grader,
                )
                self.patch(
                    experiment_id,
                    native_control={"status": "completed", "run_id": result["run_id"]},
                )
                return trials
        finally:
            if process is not None:
                self.reap(process)
            self.processes.pop(experiment_id, None)
            self.patch(experiment_id, worker=None)
            for reservation_id in outstanding | grading_outstanding:
                self.settle(
                    experiment_id, reservation_id, None, duration=time.monotonic() - started
                )
