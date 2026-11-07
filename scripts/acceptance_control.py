"""Exercise owned-worker discovery, native pause/resume, and experiment cancellation."""

import json
import time
from pathlib import Path

import httpx

from evals_app.fixtures import fixture_plan
from evals_app.models import Plan, Proposal, identifier
from evals_app.service import Service


def main():
    raw = fixture_plan(groups=2).model_dump(mode="json")
    for case in raw["cases"]:
        case["metadata"]["delay"] = 1
    plan = Plan.model_validate(raw)
    service = Service(Path("artifacts") / identifier("control-acceptance"))
    record = {}
    try:
        service.register(plan.application, key="app")
        created = service.create(
            plan,
            [
                Proposal(
                    changes={"reuse": True},
                    source="config",
                    hypothesis="reuse",
                    risk="identity changes",
                    expected_baseline=plan.application.version,
                )
            ],
            key="control",
            use_gepa=True,
        )
        service.start(created["id"], created["fingerprint"])
        deadline = time.monotonic() + 60
        server = None
        while time.monotonic() < deadline:
            server = service.runtime.discover(created["id"])
            if server:
                break
            time.sleep(0.1)
        if not server:
            raise RuntimeError("Native control discovery did not become available")
        record["discovery"] = {
            "pid": server.pid,
            "socket": str(server.socket_path),
            "api_version": server.api_version,
        }
        record["tasks"] = []
        while not record["tasks"] and time.monotonic() < deadline:
            try:
                record["tasks"] = service.runtime.native(created["id"], "tasks")
            except httpx.TransportError as error:
                # Discovery can precede the first task's blocking module initialization.
                record["startup_observation"] = type(error).__name__
            time.sleep(0.1)
        assert record["tasks"], "Native task list remained empty"
        try:
            service.runtime.native(created["id"], "requeue", sample_id="foreign-sample", epoch=1)
        except ValueError:
            record["foreign_requeue"] = "rejected"
        else:
            raise AssertionError("Foreign sample requeue was accepted")
        record["owned_requeue"] = service.runtime.native(
            created["id"], "requeue", sample_id="train-0", epoch=1
        )
        record["pause"] = service.control(created["id"], "pause")
        record["resume"] = service.control(created["id"], "resume")
        record["cancel"] = service.control(created["id"], "cancel")
        while time.monotonic() < deadline:
            experiment = service.store.read()["experiments"][created["id"]]
            if experiment["status"] in {"cancelled", "cancel_pending_confirmation", "blocked"}:
                break
            time.sleep(0.1)
        record["final"] = {k: experiment.get(k) for k in ("status", "stage", "worker", "reason")}
        assert experiment["status"] == "cancelled", record
        assert not experiment["runs"] and not experiment.get("gepa"), record
        assert "native" in record["pause"] and "native" in record["resume"], record
        print(json.dumps(record, indent=2))
    finally:
        service.close()
        Path("artifacts/control-result.json").write_text(json.dumps(record, indent=2), "utf-8")


if __name__ == "__main__":
    main()
