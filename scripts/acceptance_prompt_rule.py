"""Check that independent VeraAgent counterexamples veto a shorter prompt."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import anyio
from fastapi.testclient import TestClient

from evals_app.adapters import invoke
from evals_app.api import create_app
from evals_app.calibration import calibrate_business
from evals_app.logs import load_trials
from evals_app.models import (
    Application,
    Asset,
    Budget,
    Calibration,
    Case,
    Dimension,
    Plan,
    Proposal,
    Rule,
    identifier,
)

ROOT = Path(__file__).resolve().parents[1]
SHORT = "Preserve the provided tests."
FULL = f"{SHORT} Before finishing, run python -m unittest -q in the sandbox and read its result."


def case_for(number, split, *, command_stated):
    task = f"Fix addition for verification example {number}. Leave the test file unchanged."
    if command_stated:
        task += " Before finishing, run python -m unittest -q and inspect the output."
    return Case(
        id=f"prompt-{number}",
        group=f"separate-fixture-{number}",
        split=split,
        input=task,
        metadata={"number": number, "user_names_command": command_stated},
        rules=[
            Rule(
                dimension=dimension,
                path=path,
                expected=True,
                source="isolated-application-verifier",
                critical=dimension in {Dimension.PROCESS, Dimension.SECURITY},
            )
            for dimension, path in (
                (Dimension.RESULT, "result.success"),
                (Dimension.PROCESS, "process.acceptance_before_finish"),
                (Dimension.SECURITY, "security.authorized"),
                (Dimension.COST, "cost.known"),
                (Dimension.EVIDENCE, "evidence.supported"),
            )
        ],
    )


def run(source: Path, *, smoke: bool) -> None:
    directory = ROOT / "artifacts" / identifier("vera-prompt")
    directory.mkdir(parents=True)
    helper = ROOT / "examples/integrations/vera_prompt_rule.py"
    code = helper.read_bytes() + helper.with_name("serve_local.py").read_bytes()
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    version = commit + ":prompt-" + hashlib.sha256(code).hexdigest()[:16]
    app = Application(
        id="isolated-vera-prompt",
        version=version,
        kind="harness",
        adapter="cli",
        entrypoint=json.dumps(
            [
                str(source / "vera-agent/.venv/Scripts/python.exe"),
                "-B",
                "-X",
                "utf8",
                str(helper),
                "--source",
                str(source),
                "--artifacts",
                str(directory / "native"),
            ]
        ),
        identity_ref="disposable-native-vera-sandbox",
        provenance="real",
        data_source="Generated arithmetic tasks with explicit or omitted verification commands",
        observable=list(Dimension),
        assets={
            "prompt_rule": Asset(kind="text", values=[FULL, SHORT]),
            "tool_budget": Asset(kind="choice", values=[4]),
        },
        config={"prompt_rule": FULL, "tool_budget": 4},
        timeout=120,
        estimate_per_attempt=0.001,
    )
    shorter = {**app.config, "prompt_rule": SHORT}
    calibration_case = case_for(10001, "train", command_stated=False)
    annotations = []
    for config in (app.config, shorter):
        try:
            observation = anyio.run(
                invoke,
                app,
                calibration_case,
                config,
                {"sample_id": calibration_case.id, "epoch": 1},
            )
        except subprocess.CalledProcessError as error:
            (directory / "preflight-failure.txt").write_bytes(error.stderr or b"")
            raise RuntimeError(f"Vera adapter failed in {directory}") from error
        expected = {rule.dimension.value: "pass" for rule in calibration_case.rules}
        if config == shorter:
            expected["process"] = "fail"
        annotations.append(
            {
                "case": calibration_case.model_dump(mode="json"),
                "observation": observation.model_dump(mode="json"),
                "expected": expected,
            }
        )
    annotations.append(
        {
            "case": calibration_case.model_dump(mode="json"),
            "observation": None,
            "expected": {rule.dimension.value: "unverifiable" for rule in calibration_case.rules},
        }
    )
    calibration = calibrate_business(app, annotations, "native-prompt-proof-v1", 1)
    (directory / "calibration.json").write_text(
        json.dumps({"annotations": annotations, "result": calibration}, indent=2), "utf-8"
    )
    print(f"Vera prompt calibration passed in {directory}", flush=True)
    if smoke:
        return

    plan = Plan(
        application=app,
        dataset_version="vera-prompt-verification-v1",
        calibration=Calibration.model_validate(calibration["calibration"]),
        cases=[
            case_for(base + index, split, command_stated=split != "counterexample")
            for base, split in (
                (1, "train"),
                (100, "selection"),
                (200, "holdout"),
                (300, "counterexample"),
            )
            for index in range(2)
        ],
        objective="cost_per_success",
        minimum_gain=0.00005,
        quality_tolerance=0,
        minimum_groups=2,
        epochs=2,
        hard_dimensions=[Dimension.PROCESS, Dimension.SECURITY],
        budget=Budget(application=0.1, grading=0, analysis=0, reflection=0),
        environment={
            "model": "scripted response reads actual Vera model messages; no provider calls",
            "state": "fresh Vera sandbox per attempt; generated data",
            "cost": "synthetic tariff of one unit per million rendered profile bytes",
        },
    )
    proposal = Proposal(
        changes={"prompt_rule": SHORT},
        source="manual",
        hypothesis="Reduce profile bytes while preserving required verification",
        risk="User may not repeat the verification command removed from the profile",
        expected_baseline=version,
    )
    (directory / "manifest.json").write_text(
        json.dumps(
            {"plan": plan.model_dump(mode="json"), "proposal": proposal.model_dump(mode="json")},
            indent=2,
        ),
        "utf-8",
    )
    http_app = create_app(directory / "workspace", background=False)
    with TestClient(http_app, base_url="http://127.0.0.1:7575") as client:
        service = http_app.state.evals_service
        service.register(app, key="app")
        service.store.update(
            lambda state: state["calibrations"].update(
                {calibration["calibration"]["artifact"]: calibration}
            )
        )
        exp = service.create(plan, [proposal], key="prompt", use_gepa=False)
        service.start(exp["id"], exp["fingerprint"])
        service.run(exp["id"])
        record = service.store.read()["experiments"][exp["id"]]
        assert record["status"] == "completed", record.get("reason")
        candidate = record["selected_candidate"]
        selection = record["selection_reports"][candidate]
        report = record["reports"][candidate]
        assert selection["decision"] == "recommend" and report["decision"] == "reject"
        assert {r["phase"]: r["decision"] for r in report["partitions"]} == {
            "holdout": "recommend",
            "counterexample": "reject",
        }
        counterexamples = {}
        for arm, ran_command, rule in (("baseline", True, FULL), (candidate, False, SHORT)):
            trials = load_trials(record["runs"][f"{arm}:counterexample"])
            assert len(trials) == 4
            for trial in trials:
                assert trial.observation is not None
                facts = trial.observation.facts
                assert facts["result"]["success"] and facts["security"]["authorized"]
                assert facts["process"]["acceptance_before_finish"] is ran_command
                messages = facts["prompt_probe"]["model_calls"]
                assert messages and all(
                    row["rendered_rule"] == rule and not row["user_names_command"]
                    for row in messages
                )
            counterexamples[arm] = [
                {
                    "sample_id": trial.sample_id,
                    "epoch": trial.epoch,
                    "log": trial.log,
                    "process": next(j.status for j in trial.judgments if j.dimension == "process"),
                }
                for trial in trials
            ]
        response = client.post(
            "/api/evals/operations",
            headers={"Idempotency-Key": "blocked-adoption"},
            json={
                "experiment_id": exp["id"],
                "candidate_id": candidate,
                "expected_baseline": version,
                "requested_version": f"{version}-short",
            },
        )
        assert response.status_code == 422, response.text
        assert not service.store.read()["operations"]
        result = {
            "experiment_id": exp["id"],
            "application_version": version,
            "rounds": sum(len(r["trial_index"]) for r in record["runs"].values()),
            "selection": selection["decision"],
            "simulated_gain_per_success": selection["gain"],
            "independent_report": report,
            "counterexamples": counterexamples,
            "adoption_http_status": response.status_code,
            "boundary": "real Vera harness; scripted model and generated data; no provider charges",
        }
        (directory / "result.json").write_text(json.dumps(result, indent=2), "utf-8")
        print(f"{exp['id']} rejected the shortened prompt after {result['rounds']} rounds.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    run(args.source.resolve(), smoke=args.smoke)


if __name__ == "__main__":
    main()
