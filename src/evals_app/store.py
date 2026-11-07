"""One owning process, optimistic revisions and atomic local persistence."""

import json
import os
import threading
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

from filelock import FileLock
from tenacity import retry, retry_if_exception_type, stop_after_delay, wait_fixed

from evals_app.models import fingerprint, now


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        # ASGI lifespan teardown may run on another thread than app construction.
        self._lock = FileLock(str(self.directory / ".writer.lock"), thread_local=False)
        self._lock.acquire(timeout=0)
        # API handlers and worker supervisors both update multi-field records.
        self._mutex = threading.RLock()
        self.path = self.directory / "state.json"
        try:
            if self.path.exists():
                self.state = json.loads(self.path.read_text("utf-8"))
                if self.state.get("schema") != 1:
                    raise ValueError("Unsupported workspace schema")
            else:
                self.state = {
                    "schema": 1,
                    "revision": 0,
                    "applications": {},
                    "experiments": {},
                    "operations": {},
                    "requests": {},
                    "observations": {},
                    "strategies": {},
                    "experiences": {},
                }
                self._save(self.state)
        except BaseException:
            self._lock.release()
            raise

    def close(self) -> None:
        self._lock.release()

    def _save(self, state: dict[str, Any]) -> None:
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        self._replace(temporary)

    @retry(
        retry=retry_if_exception_type(PermissionError),
        wait=wait_fixed(0.025),
        stop=stop_after_delay(3),
        reraise=True,
    )
    def _replace(self, temporary: Path) -> None:
        # Windows readers and antivirus can briefly withhold delete sharing.
        os.replace(temporary, self.path)

    def read(self) -> dict[str, Any]:
        with self._mutex:
            return deepcopy(self.state)

    def experiment_fields(self, experiment_id: str, *names: str) -> dict[str, Any]:
        """Read supervision fields without copying all accumulated native traces."""
        with self._mutex:
            experiment = self.state["experiments"][experiment_id]
            return {name: deepcopy(experiment.get(name)) for name in names}

    def public_state(self) -> dict[str, Any]:
        """Return the workspace overview without duplicating raw execution traces."""
        with self._mutex:
            snapshot = {
                key: deepcopy(value)
                for key, value in self.state.items()
                if key not in {"requests", "experiments", "calibrations"}
            }
            snapshot["experiments"] = {
                key: {
                    name: deepcopy(value)
                    for name, value in item.items()
                    if name
                    not in {
                        "plan",
                        "runs",
                        "gepa",
                        "rescans",
                        "reservations",
                        "semantic_calibration_cases",
                    }
                }
                for key, item in self.state["experiments"].items()
            }
            return snapshot

    def experiment_view(self, experiment_id: str) -> dict[str, Any]:
        with self._mutex:
            experiment = self.state["experiments"][experiment_id]
            result = {
                key: deepcopy(value)
                for key, value in experiment.items()
                if key not in {"runs", "gepa", "rescans", "semantic_calibration_cases"}
            }
            result["runs"] = {
                key: {
                    "summary": deepcopy(run["summary"]),
                    "trials": [
                        {
                            name: deepcopy(trial[name])
                            for name in ("sample_id", "epoch", "candidate_id", "log")
                        }
                        for trial in run.get("trial_index", run.get("trials", []))
                    ],
                }
                for key, run in experiment["runs"].items()
            }
            return result

    def next_queued(self) -> str | None:
        with self._mutex:
            items = [e for e in self.state["experiments"].values() if e["status"] == "queued"]
            return min(items, key=lambda e: e["queue_order"])["id"] if items else None

    def update(
        self,
        change: Callable[[dict[str, Any]], Any],
        *,
        revision: int | None = None,
        key: str | None = None,
        payload: Any = None,
    ) -> Any:
        with self._mutex:
            if key and key in self.state["requests"]:
                request = self.state["requests"][key]
                if request["fingerprint"] != fingerprint(payload):
                    raise Conflict("Idempotency key was reused with a different payload")
                return deepcopy(request["result"])
            if revision is not None and revision != self.state["revision"]:
                raise Conflict("Workspace revision changed; refresh before retrying")
            state = deepcopy(self.state)
            result = change(state)
            state["revision"] += 1
            state["updated_at"] = now()
            if key:
                state["requests"][key] = {"fingerprint": fingerprint(payload), "result": result}
            self._save(state)
            self.state = state
            return deepcopy(result)
