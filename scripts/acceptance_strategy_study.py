"""Exercise all three strategy arms with actual Inspect/GEPA runs on a small fixture."""

import argparse
import json
from pathlib import Path

from fastapi.testclient import TestClient

from evals_app.api import create_app
from evals_app.fixtures import fixture_plan
from evals_app.models import Plan
from evals_app.policies import BUILTIN_POLICY, SIMPLE_POLICY, SearchPolicy


def run(directory: Path) -> dict:
    directory.mkdir(parents=True)
    raw = fixture_plan(groups=2).model_dump(mode="json")
    raw.update(epochs=1, search_metric_calls=6)
    raw["application"]["assets"]["policy"]["values"] = ["strict"]
    raw["application"]["assets"]["top_k"]["values"] = [2]
    plan = Plan.model_validate(raw)
    new_policy = SearchPolicy(version="text-with-config-v2", configuration_candidates=True)
    policies = {"old": BUILTIN_POLICY, "simple_rule": SIMPLE_POLICY, "new": new_policy}
    app = create_app(directory / "workspace", background=False)
    with TestClient(app, base_url="http://127.0.0.1:7575") as client:
        service = app.state.evals_service

        def post(path, body=None, key=None):
            response = client.post(
                f"/api/evals/{path}",
                json=body,
                headers={"Idempotency-Key": key} if key else None,
            )
            response.raise_for_status()
            return response.json()

        post("applications", plan.application.model_dump(mode="json"), "app")
        experiments = {
            name: post(
                "experiments",
                {"plan": plan.model_dump(mode="json"), "policy": policy.model_dump(mode="json")},
                name,
            )
            for name, policy in policies.items()
        }
        study = post(
            "strategies",
            {
                "version": new_policy.version,
                "arms": {name: [record["id"]] for name, record in experiments.items()},
                "budget_per_arm": 20,
            },
            "study",
        )
        assert study["promotion_eligible"] is True
        run_evidence = {}
        for name, record in experiments.items():
            post(f"experiments/{record['id']}/start", {"fingerprint": record["fingerprint"]})
            service.run(record["id"])
            experiment = service.store.read()["experiments"][record["id"]]
            assert experiment["status"] == "completed", experiment.get("reason")
            batches = len(experiment.get("gepa", {}).get("history", []))
            assert bool(batches) == policies[name].use_gepa
            run_evidence[name] = {
                "experiment_id": record["id"],
                "native_runs": len(experiment["runs"]),
                "gepa_batches": batches,
                "decisions": {k: v["decision"] for k, v in experiment["reports"].items()},
            }
            print(f"{name}: {run_evidence[name]}", flush=True)
        report = post(f"strategies/{study['id']}/evaluate")
        assert report["decision"] == "insufficient_evidence"
        assert "Too few independent paired strategy units" in report["reasons"]
        assert all(0 < row["search_cost"] <= 20 for row in report["results"].values())
        assert report["results"]["old"]["verified_improvements"] == 0
        assert report["results"]["simple_rule"]["verified_improvements"] == 1
        assert report["results"]["new"]["verified_improvements"] == 1
        response = client.post(
            f"/api/evals/strategies/{study['id']}/promote",
            json={"expected_default_version": BUILTIN_POLICY.version},
            headers={"Idempotency-Key": "insufficient"},
        )
        assert response.status_code == 422
        later = post("experiments", {"plan": plan.model_dump(mode="json")}, "later")
        saved = service.store.experiment_fields(later["id"], "policy")
        assert saved["policy"] == BUILTIN_POLICY.model_dump(mode="json")
        result = {
            "report": report,
            "run_evidence": run_evidence,
            "promotion_http_status": response.status_code,
            "default_after_study": saved["policy"]["version"],
            "boundary": "Generated fixtures and simulated costs, one paired unit",
        }
        (directory / "result.json").write_text(json.dumps(result, indent=2), "utf-8")
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    result = run(args.directory.resolve())
    print(json.dumps({"study": result["report"]["id"], "decision": result["report"]["decision"]}))


if __name__ == "__main__":
    main()
