import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import anyio
import httpx
from fastapi.testclient import TestClient
from inspect_ai.log import read_eval_log

from evals_app import coordination, worker
from evals_app.api import create_app
from evals_app.fixtures import fixture_plan, synthetic_application
from evals_app.logs import load_trials
from evals_app.models import Plan, Status, now
from evals_app.service import Service


def test_call_gate_reopens_temporarily_locked_reply_without_republishing(tmp_path, monkeypatch):
    writes = []
    reads = []
    publish = coordination.atomic_json
    read_text = Path.read_text

    def respond(path, value):
        writes.append(value)
        publish(path, value)
        reply = path.with_name(path.name.replace(".request.json", ".reply.json"))
        publish(reply, {"settled": True})

    def temporarily_locked(path, *args, **kwargs):
        if path.name.endswith(".reply.json"):
            reads.append(path)
            if len(reads) == 1:
                raise PermissionError(13, "Injected Windows file-sharing denial")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(coordination, "atomic_json", respond)
    monkeypatch.setattr(Path, "read_text", temporarily_locked)
    anyio.run(coordination.CallGate(tmp_path).settle, "reservation-1", 0.2, 1.0)
    assert len(writes) == 1 and writes[0]["reservation_id"] == "reservation-1"
    assert len(reads) == 2 and reads[0] == reads[1]


def test_original_logs_preserve_unscored_native_sample_rounds(tmp_path, monkeypatch):
    plan = fixture_plan(groups=2)
    cases = [case for case in plan.cases if case.split == "train"]
    execute = worker.execute

    async def fail_one(application, case, *args, **kwargs):
        if case.id == cases[0].id:
            raise PermissionError(13, "Injected native failure before scoring")
        return await execute(application, case, *args, **kwargs)

    monkeypatch.setattr(worker, "execute", fail_one)
    result = worker.run_job(
        {
            "plan": {
                "application": plan.application.model_dump(mode="json"),
                "epochs": 1,
                "platform_retries": 0,
                "retryable": [],
            },
            "cases": [case.model_dump(mode="json") for case in cases],
            "config": plan.application.config,
            "candidate_id": "baseline",
            "experiment_id": "native-error",
            "plan_fingerprint": "test",
            "log_dir": str(tmp_path),
        }
    )
    run = {
        "candidate_id": "baseline",
        "log": result["log"],
        "trial_index": [
            {"sample_id": item["sample_id"], "epoch": item["epoch"]} for item in result["trials"]
        ],
    }
    native = read_eval_log(run["log"])
    failed_sample = next(sample for sample in native.samples if sample.error)
    assert failed_sample.id == cases[0].id
    expected = [trial.model_dump(mode="json") for trial in load_trials(run)]
    assert expected == result["trials"]
    assert all(
        judgment["status"] in {"unverifiable", "not_applicable"}
        for trial in expected
        if trial["sample_id"] == cases[0].id
        for judgment in trial["judgments"]
    )

    # Older native logs retained the failed disposition in the worker receipt.
    failed_sample.metadata.pop("evals_judgments")
    failed_sample.metadata.pop("evals_config_fingerprint")
    Path(run["log"]).with_name("job.result.json").write_text(json.dumps(result), "utf-8")
    monkeypatch.setattr("evals_app.logs.read_eval_log", lambda _: native)
    assert [trial.model_dump(mode="json") for trial in load_trials(run)] == expected


def test_budget_is_checked_before_each_actual_application_call(tmp_path):
    raw = fixture_plan(groups=2).model_dump(mode="json")
    raw["application"]["estimate_per_attempt"] = 0.1
    raw["budget"]["application"] = 0.15
    plan = Plan.model_validate(raw)
    service = Service(tmp_path, background=False)
    try:
        service.register(plan.application, key="app")
        created = service.create(plan, [], key="budget", use_gepa=False)
        service.start(created["id"], created["fingerprint"])
        trials = service._evaluate(
            created["id"], plan, {"id": "baseline", "config": plan.application.config}, "train"
        )
        assert len(trials) == 4
        assert sum(len(t.attempts) for t in trials) == 1
        saved = service.store.read()["experiments"][created["id"]]["runs"]["baseline:train"]
        assert "trials" not in saved
        assert read_eval_log(saved["log"]).eval.metadata["build"] == service.build
        restored = service._evaluate(
            created["id"], plan, {"id": "baseline", "config": plan.application.config}, "train"
        )
        assert restored == trials
        assert sum(t.observation is not None for t in trials) == 1
        assert sum(any(j.status == Status.NOT_RUN for j in t.judgments) for t in trials) == 3
        ledger = service.store.read()["experiments"][created["id"]]["ledger"]["application"]
        assert ledger == {"spent": 0.2, "reserved": 0, "unknown": 0}
    finally:
        service.close()


def test_committed_http_write_with_missing_response_requires_reconciliation(tmp_path, monkeypatch):
    committed = {}
    resets = []
    release_response = Event()

    class Endpoint(BaseHTTPRequestHandler):
        def send_json(self, value, status=200):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def do_GET(self):
            attempt_id = self.path.removeprefix("/receipt/")
            if attempt_id not in committed:
                self.send_error(404)
                return
            self.send_json(committed[attempt_id])

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            context = payload["context"]
            attempt_id = context["attempt_id"]
            if self.path == "/reset":
                resets.append(context["sample_id"])
                self.send_json({})
                return
            result = anyio.run(synthetic_application, payload)
            committed[attempt_id] = {"sample": context["sample_id"], "cost": result["cost"]}
            if context["sample_id"] == "train-0":
                release_response.wait(20)
                return
            self.send_json(result)

        def log_message(self, *args):
            pass

    (tmp_path / "remote_write_fixture.py").write_text(
        "import json, os, urllib.request\n"
        "def reset(payload):\n"
        "    request = urllib.request.Request(\n"
        "        os.environ['EVALS_REMOTE_WRITE_URL'] + '/reset',\n"
        "        data=json.dumps(payload).encode())\n"
        "    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(\n"
        "        request, timeout=1\n"
        "    ) as response:\n"
        "        response.read()\n",
        "utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path), prepend=os.pathsep)
    with ThreadingHTTPServer(("127.0.0.1", 0), Endpoint) as server:
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        monkeypatch.setenv("EVALS_REMOTE_WRITE_URL", base)
        raw = fixture_plan(groups=2).model_dump(mode="json")
        raw.update(epochs=1, platform_retries=1, retryable=["TimeoutError", "ReadTimeout"])
        raw["application"].update(
            adapter="http",
            entrypoint=f"{base}/write",
            timeout=2,
            read_only=False,
            reset_entrypoint="remote_write_fixture:reset",
        )
        plan = Plan.model_validate(raw)
        app = create_app(tmp_path / "workspace", background=False)
        try:
            with TestClient(app, base_url="http://127.0.0.1:7575") as client:
                service = app.state.evals_service
                service.register(plan.application, key="app")
                exp = service.create(plan, [], key="first", use_gepa=False)
                service.start(exp["id"], exp["fingerprint"])
                service.run(exp["id"])
                record = service.store.read()["experiments"][exp["id"]]
                assert record["status"] == "blocked", record.get("reason")
                assert "Remote write requires state and cost verification" in record["reason"]
                assert set(record["runs"]) == {"baseline:train"}
                failed, skipped = load_trials(record["runs"]["baseline:train"])
                (attempt,) = failed.attempts
                assert attempt["error"] in {"TimeoutError", "ReadTimeout"}
                assert attempt["cost"] is None and not skipped.attempts
                receipt_url = f"{base}/receipt/{attempt['attempt_id']}"
                receipt_response = httpx.get(receipt_url, trust_env=False)
                receipt_response.raise_for_status()
                receipt = receipt_response.json()
                assert receipt == {"sample": "train-0", "cost": 0.2}
                assert list(committed) == [attempt["attempt_id"]]
                (reservation_id,) = record["reservations"]
                assert record["reservations"][reservation_id]["status"] == "unknown"

                later = service.create(plan, [], key="later", use_gepa=False)
                service.start(later["id"], later["fingerprint"])
                service.run(later["id"])
                later_record = service.store.read()["experiments"][later["id"]]
                assert later_record["status"] == "blocked" and not later_record["runs"]
                assert list(committed) == [attempt["attempt_id"]]

                route = f"/api/evals/experiments/{exp['id']}/recover"
                body = {
                    "fingerprint": exp["fingerprint"],
                    "evidence": {
                        "source": "isolated write ledger",
                        "locator": receipt_url,
                        "collected_at": now(),
                    },
                    "costs": {},
                }
                assert client.post(route, json=body).status_code == 422
                body["costs"] = {reservation_id: receipt["cost"]}
                assert client.post(route, json=body).json()["status"] == "queued"
                release_response.set()
                service.run(exp["id"])
                resumed = service.store.read()["experiments"][exp["id"]]
                assert resumed["status"] == "completed", resumed.get("reason")
                assert resumed["runs"]["baseline:train"] == record["runs"]["baseline:train"]
                assert resumed["ledger"]["application"]["unknown"] == 0
                assert [item["sample"] for item in committed.values()].count("train-0") == 1
                assert resets.count("train-0") == 1
        finally:
            release_response.set()
            server.shutdown()
            server_thread.join(timeout=5)
