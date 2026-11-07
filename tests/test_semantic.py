import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import anyio
import pytest
from fastapi.testclient import TestClient
from inspect_ai.log import read_eval_log
from inspect_ai.model import ModelOutput, ModelUsage
from pydantic import ValidationError

from evals_app.api import create_app
from evals_app.fixtures import fixture_plan, synthetic_application
from evals_app.logs import load_trials
from evals_app.models import Evidence, Observation, Plan, SemanticAnalyzer, Trial, fingerprint, now
from evals_app.semantic import SemanticValidationCase, permitted_row, schedule_calibration
from evals_app.semantic_worker import actual_cost, create_gated_model
from evals_app.service import Service
from evals_app.store import Conflict


def definition(**changes):
    return SemanticAnalyzer.model_validate(
        {
            "id": "support-check",
            "version": "v1",
            "question": "Does the evidence suggest a problem?",
            "evidence_paths": ["evidence.supported"],
            "model": "mockllm/model",
            "model_version": "mockllm",
            "estimate_per_call": 0,
            "pricing_version": "fixture-zero-v1",
            "cost_source": "unbilled mock provider",
            "calibration_id": "support-calibration-v1",
            "calibration_dataset_version": "separate-labels-v1",
            "fixture_replies": [
                "Evidence [M1] suggests a problem.\nANSWER: yes",
                "No problem.\nANSWER: no",
            ],
            **changes,
        }
    )


def annotations():
    return [
        SemanticValidationCase(
            id=f"label-{index}",
            group=f"calibration-source-{index}",
            text=text,
            reference=Evidence(
                source="business-annotator",
                locator=f"annotations#{index}",
                version="v1",
                scope="local",
                collected_at=now(),
            ),
            expected=label,
        )
        for index, (text, label) in enumerate(
            (
                ("A claim contradicts its source", "yes"),
                ("The answer agrees with the authorized source", "no"),
                (None, "unverifiable"),
            )
        )
    ]


def grading_plan():
    raw = fixture_plan(groups=2).model_dump(mode="json")
    raw.update(
        epochs=1,
        semantic_grader=definition(
            id="grounding-grade",
            calibration_id="business-grade-v1",
            fixture_replies=[
                "The evidence agrees [M1].\nANSWER: yes",
                "Contrary evidence [M1].\nANSWER: no",
            ],
        ).model_dump(mode="json"),
    )
    for case in raw["cases"]:
        rule = next(rule for rule in case["rules"] if rule["dimension"] == "evidence")
        rule.update(operator="semantic", expected="yes")
    return raw


def test_separately_calibrated_grader_supplies_native_business_scores(tmp_path):
    plan = Plan.model_validate(grading_plan())
    app = create_app(tmp_path, background=False)
    service = app.state.evals_service
    with TestClient(app, base_url="http://127.0.0.1:7575") as client:
        service.register(plan.application, key="app")
        created = service.create(plan, [], key="graded", use_gepa=False)
        experiment_id = created["id"]
        start = f"/api/evals/experiments/{experiment_id}/start"
        body = {"fingerprint": created["fingerprint"]}
        assert client.post(start, json=body).status_code == 422
        labels = {"cases": [case.model_dump(mode="json") for case in annotations()]}
        assert (
            client.post(
                f"/api/evals/experiments/{experiment_id}/analyzer-calibration",
                json=labels,
                headers={"Idempotency-Key": "labels"},
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"/api/evals/experiments/{experiment_id}/grader-calibration",
                json=labels,
                headers={"Idempotency-Key": "labels"},
            ).status_code
            == 200
        )
        service.run(experiment_id)
        assert client.get("/api/evals/analyzer-calibrations/business-grade-v1").status_code == 404
        record = client.get("/api/evals/grader-calibrations/business-grade-v1").json()
        assert (
            record["correct"] == 3 and record["role"] == "grading" and record["status"] == "passed"
        )
        assert client.post(start, json=body).status_code == 200
        service.run(experiment_id)
        experiment = service.store.read()["experiments"][experiment_id]
        assert experiment["status"] == "completed", experiment.get("reason")
        run = experiment["runs"]["baseline:train"]
        evidence = [
            j for trial in load_trials(run) for j in trial.judgments if j.dimension == "evidence"
        ]
        assert {j.status for j in evidence} == {"pass", "fail"}
        assert all(
            j.scorer_version == "semantic:grounding-grade:v1" and j.evidence for j in evidence
        )
        log = read_eval_log(run["log"])
        for sample in log.samples:
            grade = next(iter(sample.scores.values())).metadata["semantic_grade"]
            assert grade["result"]["references"] and grade["calibration_id"] == "business-grade-v1"
        graded = [
            row for row in experiment["reservations"].values() if row["category"] == "grading"
        ]
        assert len(graded) == 6
        assert all(row["actual"] == 0 and row["usage"]["model"] == "mockllm" for row in graded)
        assert all(
            row["usage"] is None
            for row in experiment["reservations"].values()
            if row["category"] == "application"
        )


def test_semantic_labels_cannot_replace_hard_authorization_checks():
    raw = grading_plan()
    raw["hard_dimensions"].append("evidence")
    with pytest.raises(ValidationError, match="non-hard"):
        Plan.model_validate(raw)


@pytest.mark.parametrize("provider", ["openai", "ollama"])
def test_loopback_judge_ignores_proxies_without_altering_application_environment(
    tmp_path, monkeypatch, provider
):
    seen = []

    class Endpoint(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            if self.path == "/redirect":
                self.send_response(307)
                self.send_header("Location", "/would-forward-evidence")
            else:
                self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Endpoint) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.setenv("NO_PROXY", "")
        config = definition(
            model=f"{provider}/local-judge",
            model_version="local-judge",
            base_url=f"{url}/v1",
            fixture_replies=None,
        )

        async def check():
            async with create_gated_model(config, tmp_path) as model:
                assert model.api.client.max_retries == 0
                assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:9"
                assert os.environ["NO_PROXY"] == ""
                assert (await model.api.http_client.get(f"{url}/redirect")).status_code == 307

        try:
            anyio.run(check)
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert seen == ["/redirect"]


def test_cancelling_live_semantic_analysis_reaps_worker_before_search(tmp_path):
    analyzing = Event()
    entered = Event()
    release = Event()
    requests = []

    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request["model"])
            if analyzing.is_set():
                entered.set()
                release.wait(30)
                return
            content = (
                "Evidence [M1] supports the concern.\nANSWER: yes"
                if len(requests) == 1
                else "No concern.\nANSWER: no"
            )
            envelope = {"id": "fixture", "created": 1, "model": "local-judge"}
            usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
            if request.get("stream"):
                chunk = {**envelope, "object": "chat.completion.chunk"}
                events = [
                    {
                        **chunk,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": content},
                                "finish_reason": "stop",
                            }
                        ],
                    },
                    {**chunk, "choices": [], "usage": usage},
                ]
                response = (
                    "".join(f"data: {json.dumps(event)}\n\n" for event in events)
                    + "data: [DONE]\n\n"
                ).encode()
                content_type = "text/event-stream"
            else:
                response = json.dumps(
                    {
                        **envelope,
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": content},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": usage,
                    }
                ).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Endpoint) as server:
        listener = Thread(target=server.serve_forever, daemon=True)
        listener.start()
        analyzer = definition(
            model="ollama/local-judge",
            model_version="local-judge",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            fixture_replies=None,
            input_per_million=0,
            output_per_million=0,
            timeout=60,
        )
        raw = fixture_plan(groups=2).model_dump(mode="json")
        raw.update(epochs=1, semantic_analyzer=analyzer.model_dump(mode="json"))
        plan = Plan.model_validate(raw)
        service = Service(tmp_path / "workspace", background=False)
        supervisor = None
        try:
            service.register(plan.application, key="app")
            created = service.create(plan, [], key="analysis", use_gepa=True)
            experiment_id = created["id"]
            schedule_calibration(service, experiment_id, annotations(), key="labels")
            service.run(experiment_id)
            calibration = service.store.read()["analyzer_calibrations"][analyzer.calibration_id]
            assert calibration["status"] == "passed", calibration
            assert requests == ["local-judge"] * 2
            analyzing.set()
            service.start(experiment_id, created["fingerprint"])
            supervisor = Thread(target=service.run, args=(experiment_id,))
            supervisor.start()
            assert entered.wait(90), service.store.experiment_fields(
                experiment_id, "status", "reason"
            )
            assert (
                service.store.experiment_fields(experiment_id, "stage")["stage"]
                == "semantic_analysis"
            )
            worker = service.runtime.processes[experiment_id]
            service.control(experiment_id, "cancel")
            supervisor.join(15)
            assert not supervisor.is_alive()
            assert worker.poll() is not None
            experiment = service.store.read()["experiments"][experiment_id]
            assert experiment["status"] == "cancelled"
            assert experiment["worker"] is None and "gepa" not in experiment
            assert set(experiment["runs"]) == {"baseline:train", "baseline:selection"}
            assert requests == ["local-judge"] * 3
            assert experiment["ledger"]["analysis"]["unknown"] == 1
        finally:
            release.set()
            if supervisor and supervisor.is_alive():
                service.control(experiment_id, "cancel")
                supervisor.join(15)
            service.close()
            server.shutdown()
            listener.join(5)


def test_scout_calibration_and_runtime_analysis_use_owner_budgets(tmp_path):
    raw = fixture_plan(groups=2).model_dump(mode="json")
    raw.update(epochs=1, semantic_analyzer=definition().model_dump(mode="json"))
    plan = Plan.model_validate(raw)
    service = Service(tmp_path, background=False)
    try:
        service.register(plan.application, key="app")
        created = service.create(plan, [], key="exp", use_gepa=False)
        with pytest.raises(ValueError, match="calibration"):
            service.start(created["id"], created["fingerprint"])
        labels = annotations()
        scheduled = schedule_calibration(service, created["id"], labels, key="labels")
        assert schedule_calibration(service, created["id"], labels, key="labels") == scheduled
        service.run(created["id"])
        state = service.store.read()
        calibrated = state["analyzer_calibrations"][plan.semantic_analyzer.calibration_id]
        assert calibrated["status"] == "passed", state["experiments"][created["id"]].get("reason")
        assert calibrated["correct"] == calibrated["expected"] == 3
        assert calibrated["provenance"] == "fixture"
        assert calibrated["results"][0]["result"]["references"]
        with pytest.raises(Conflict, match="immutable"):
            schedule_calibration(service, created["id"], annotations(), key="rewritten-times")
        service.start(created["id"], created["fingerprint"])
        service.run(created["id"])
        experiment = service.store.read()["experiments"][created["id"]]
        assert experiment["status"] == "completed", experiment.get("reason")
        semantic = [r for r in experiment["findings"] if r.get("model")]
        assert len(semantic) == 1 and semantic[0]["scanner_references"]
        assert all(
            r["usage"]["pricing_version"] == "fixture-zero-v1"
            for r in experiment["reservations"].values()
            if r.get("usage")
        )
        assert experiment["ledger"]["analysis"] == {"spent": 0, "reserved": 0, "unknown": 0}
    finally:
        service.close()


def test_invalid_calibration_cannot_enable_search(tmp_path):
    raw = fixture_plan(groups=2).model_dump(mode="json")
    raw["semantic_analyzer"] = definition(
        fixture_replies=["No problem.\nANSWER: no", "No problem.\nANSWER: no"]
    ).model_dump(mode="json")
    plan = Plan.model_validate(raw)
    service = Service(tmp_path, background=False)
    try:
        service.register(plan.application, key="app")
        created = service.create(plan, [], key="bad", use_gepa=False)
        schedule_calibration(service, created["id"], annotations(), key="bad-labels")
        service.run(created["id"])
        with pytest.raises(ValueError, match="calibration"):
            service.start(created["id"], created["fingerprint"])
        assert not service.store.read()["experiments"][created["id"]]["runs"]
    finally:
        service.close()


@pytest.mark.parametrize(
    "changes",
    [
        {
            "model": "openai/private",
            "model_version": "private",
            "base_url": "https://external.example/v1",
        },
        {"deployment": "external"},
        {"evidence_paths": ["x", "x"]},
    ],
)
def test_unsafe_or_ambiguous_analyzer_contracts_are_rejected(changes):
    with pytest.raises(ValidationError):
        definition(**changes)


def test_token_pricing_preserves_unknowns_and_model_identity():
    config = definition(input_per_million=2, output_per_million=4)
    output = ModelOutput.from_content("mockllm", "answer").model_copy(
        update={"usage": ModelUsage(input_tokens=100, output_tokens=50, input_tokens_cache_read=20)}
    )
    assert actual_cost(output, config) is None
    config = config.model_copy(update={"cache_read_per_million": 0.2})
    assert actual_cost(output, config) == pytest.approx(0.000404)
    assert actual_cost(output.model_copy(update={"model": "other"}), config) is None
    invalid_usage = ModelUsage(total_cost=0).model_copy(update={"input_tokens": -1})
    assert actual_cost(output.model_copy(update={"usage": invalid_usage}), config) is None


def test_external_semantic_inputs_exclude_unapproved_values():
    config = definition(
        model="openai/judge-v1",
        model_version="judge-v1",
        deployment="external",
        identity_ref="JUDGE_KEY",
        estimate_per_call=0.01,
        fixture_replies=None,
    )
    plan = fixture_plan(groups=2).model_copy(update={"semantic_analyzer": config})
    observation = Observation.model_validate(
        anyio.run(
            synthetic_application,
            {
                "config": plan.application.config,
                "metadata": plan.cases[0].metadata,
                "application_version": plan.application.version,
            },
        )
    )
    trial = Trial(
        sample_id="train",
        group="train",
        epoch=1,
        attempt_id="1",
        candidate_id="baseline",
        run_id="run",
        config_fingerprint=fingerprint(plan.application.config),
        application_version=plan.application.version,
        observation=observation,
        judgments=[],
        attempts=[],
        log="local.eval",
    )
    with pytest.raises(ValueError, match="authorize external"):
        permitted_row(plan, trial)
    plan = plan.model_copy(
        update={"application": plan.application.model_copy(update={"evidence_scope": "shareable"})}
    )
    local_ref = observation.evidence["evidence.supported"].model_copy(update={"scope": "local"})
    trial = trial.model_copy(
        update={
            "observation": observation.model_copy(
                update={
                    "evidence": {"evidence.supported": local_ref},
                    "facts": {"evidence": {"supported": "PRIVATE"}},
                }
            )
        }
    )
    assert permitted_row(plan, trial)["text"] is None
    assert "PRIVATE" not in json.dumps(permitted_row(plan, trial))

    composite = config.model_copy(update={"evidence_paths": ["evidence"]})
    plan = plan.model_copy(update={"semantic_analyzer": composite})
    trial = trial.model_copy(
        update={
            "observation": trial.observation.model_copy(
                update={
                    "evidence": {
                        "evidence": local_ref.model_copy(update={"scope": "shareable"}),
                        "evidence.supported": local_ref,
                    },
                }
            )
        }
    )
    assert permitted_row(plan, trial)["text"] is None
    assert "PRIVATE" not in json.dumps(permitted_row(plan, trial))


def test_calibration_api_rejects_data_leakage_and_preserves_idempotency(tmp_path):
    plan = fixture_plan(groups=2).model_copy(update={"semantic_analyzer": definition()})
    app = create_app(tmp_path, background=False)
    service = app.state.evals_service
    with TestClient(app, base_url="http://127.0.0.1:7575") as client:
        service.register(plan.application, key="app")
        created = service.create(plan, [], key="api", use_gepa=False)
        path = f"/api/evals/experiments/{created['id']}/analyzer-calibration"
        labels = annotations()
        bad = labels.copy()
        bad[0] = bad[0].model_copy(update={"group": plan.cases[0].group})
        rejected = client.post(
            path,
            json={"cases": [c.model_dump(mode="json") for c in bad]},
            headers={"Idempotency-Key": "leaking"},
        )
        assert rejected.status_code == 422 and "distinct" in rejected.text
        assert not service.store.read().get("analyzer_calibrations")
        body = {"cases": [c.model_dump(mode="json") for c in labels]}
        accepted = client.post(path, json=body, headers={"Idempotency-Key": "labels"})
        assert accepted.status_code == 200, accepted.text
        assert (
            client.post(path, json=body, headers={"Idempotency-Key": "labels"}).json()
            == accepted.json()
        )
        record = client.get(f"/api/evals/analyzer-calibrations/{definition().calibration_id}")
        assert record.status_code == 200 and record.json()["status"] == "queued"
        assert "semantic_calibration_cases" not in client.get("/api/evals/state").text
        assert client.get("/api/evals/analyzer-calibrations/foreign").status_code == 404
        service.control(created["id"], "cancel")
