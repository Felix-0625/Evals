from urllib.parse import quote

from fastapi.testclient import TestClient

from evals_app.api import create_app
from evals_app.fixtures import fixture_plan


def test_http_shared_workflow_origin_ownership_and_restart(tmp_path):
    plan = fixture_plan()
    app = create_app(tmp_path, background=False)
    with TestClient(app, base_url="http://127.0.0.1:7575") as client:
        assert client.get("/").status_code == 200
        assert (
            client.get(
                "/api/evals/state", headers={"Origin": "https://untrusted.example"}
            ).status_code
            == 403
        )
        registered = client.post(
            "/api/evals/applications",
            json=plan.application.model_dump(mode="json"),
            headers={"Idempotency-Key": "register"},
        )
        assert registered.status_code == 200
        body = {"plan": plan.model_dump(mode="json"), "use_gepa": False}
        created = client.post(
            "/api/evals/experiments", json=body, headers={"Idempotency-Key": "create"}
        )
        assert created.status_code == 200, created.text
        experiment_id = created.json()["id"]
        assert (
            client.post(
                "/api/evals/experiments", json=body, headers={"Idempotency-Key": "create"}
            ).json()
            == created.json()
        )
        assert client.get("/api/evals/experiments/another-workspace").status_code == 404
        assert client.get("/api/evals/experiments/another-workspace/native").status_code == 404
        outside = (tmp_path.parent / "secret.eval").as_uri()
        assert client.get(f"/api/logs/{quote(outside, safe='')}").status_code == 403
        wrong = client.post(
            f"/api/evals/experiments/{experiment_id}/start", json={"fingerprint": "wrong"}
        )
        assert wrong.status_code == 409
        assert (
            client.post(f"/api/evals/experiments/{experiment_id}/control/cancel").status_code == 200
        )
        assert client.get(f"/api/evals/experiments/{experiment_id}").json()["status"] == "cancelled"
    restored = create_app(tmp_path, background=False)
    with TestClient(restored, base_url="http://127.0.0.1:7575") as client:
        assert client.get(f"/api/evals/experiments/{experiment_id}").json()["status"] == "cancelled"
