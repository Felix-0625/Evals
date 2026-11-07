"""Actual NexRAG boundary cases through the same CLI adapter and Inspect workflow."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import anyio

from evals_app.adapters import invoke
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
from evals_app.service import Service

ROOT = Path(__file__).resolve().parents[1]
NEX_SCENARIOS = [
    "legitimate",
    "cross_tenant",
    "unauthorized_document",
    "revocation",
    "prompt_injection",
    "dangerous_write",
    "tool_failure",
]


def case_for(scenario, number, split, prefix="native"):
    return Case(
        id=f"{prefix}-{scenario}-{number}",
        group=f"{prefix}-group-{number}",
        split=split,
        input=f"Generated {scenario} task {number}; verify native authorization and business state",
        metadata={"scenario": scenario, "number": number},
        rules=[
            Rule(
                dimension=dimension,
                path=path,
                expected=True,
                critical=dimension == Dimension.SECURITY,
                source="isolated-application-verifier",
            )
            for dimension, path in [
                (Dimension.RESULT, "result.success"),
                (Dimension.SECURITY, "security.authorized"),
                (Dimension.RECOVERY, "recovery.consistent"),
                (Dimension.COST, "cost.known"),
            ]
        ],
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--kind", choices=["nexrag", "vera"], default="nexrag")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--compare-tool-budget", action="store_true")
    parser.add_argument("--provider-retry", action="store_true")
    args = parser.parse_args()
    if args.compare_tool_budget and (args.kind != "vera" or args.preflight):
        parser.error("Tool-budget comparison requires a new VeraAgent workspace")
    if args.provider_retry and (args.kind != "vera" or args.compare_tool_budget):
        parser.error("Provider retry requires VeraAgent with a fixed tool budget")
    source = args.source.resolve()
    scenarios = (
        NEX_SCENARIOS
        if args.kind == "nexrag"
        else ["tool_failure", "duplicate_edit", "changed_data"]
    )
    if args.provider_retry:
        scenarios = ["provider_timeout", "provider_rate_limit"]
    run_name = "vera-provider-retry" if args.provider_retry else f"{args.kind}-boundaries"
    directory = (
        args.preflight.resolve() if args.preflight else ROOT / "artifacts" / identifier(run_name)
    )
    directory.mkdir(parents=True, exist_ok=args.preflight is not None)
    helper = (
        ROOT
        / "examples/integrations"
        / ("nexrag_boundaries.py" if args.kind == "nexrag" else "vera_faults.py")
    )
    if args.provider_retry:
        helper = helper.with_name("vera_provider_retry.py")
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    code = helper.read_bytes()
    if args.kind == "vera":
        code += helper.with_name("serve_local.py").read_bytes()
    version = (
        commit
        + (
            ":retry-"
            if args.provider_retry
            else ":boundaries-"
            if args.kind == "nexrag"
            else ":faults-"
        )
        + hashlib.sha256(code).hexdigest()[:16]
    )
    app = Application(
        id=f"isolated-{args.kind}-boundaries",
        version=version,
        kind="agent" if args.kind == "nexrag" else "harness",
        adapter="cli",
        entrypoint=json.dumps(
            [
                str(
                    source
                    / (
                        ".venv/Scripts/python.exe"
                        if args.kind == "nexrag"
                        else "vera-agent/.venv/Scripts/python.exe"
                    )
                ),
                "-X",
                "utf8",
                str(helper),
                "--source",
                str(source),
                "--artifacts",
                str(directory / "native"),
            ]
        ),
        identity_ref="generated-signed-local-identity"
        if args.kind == "nexrag"
        else "isolated-sandbox-identity",
        provenance="real",
        executor="local",
        data_source=(
            "Generated SQLite tenants; temporary OpsStore; "
            "local ACL bridge; scripted adversarial model"
        )
        if args.kind == "nexrag"
        else "Generated addition fixtures; native temporary Vera sandbox; scripted model",
        observable=list(Dimension),
        assets={}
        if args.kind == "nexrag"
        else {
            "tool_budget": Asset(kind="choice", values=[1, 4] if args.compare_tool_budget else [4]),
            "prompt_rule": Asset(kind="text", values=[""]),
        },
        config={}
        if args.kind == "nexrag"
        else {
            "tool_budget": 1 if args.compare_tool_budget else 4,
            "prompt_rule": "",
        },
        timeout=120,
        evidence_scope="shareable",
        estimate_per_attempt=0,
    )
    preflight = (
        json.loads((directory / "preflight.json").read_text("utf-8")) if args.preflight else []
    )
    positive_config = {**app.config, "tool_budget": 4} if args.compare_tool_budget else app.config
    if preflight and (
        [row["case"]["metadata"]["scenario"] for row in preflight] != scenarios
        or any(row["observation"]["application_version"] != version for row in preflight)
    ):
        raise ValueError("Preflight source/adapter version or scenarios changed")
    for index, scenario in enumerate([] if args.preflight else scenarios):
        case = case_for(scenario, 10000 + index, "train", "calibration")
        try:
            observation = anyio.run(
                invoke, app, case, positive_config, {"sample_id": case.id, "epoch": 1}
            )
        except subprocess.CalledProcessError as error:
            (directory / "preflight-failure.txt").write_bytes(error.stderr or b"")
            raise RuntimeError(
                f"Native adapter failed; see {directory / 'preflight-failure.txt'}"
            ) from error
        preflight.append(
            {
                "case": case.model_dump(mode="json"),
                "observation": observation.model_dump(mode="json"),
            }
        )
        print(
            json.dumps(
                {
                    "scenario": scenario,
                    "result": observation.facts["result"],
                    "security": observation.facts["security"],
                }
            ),
            flush=True,
        )
    (directory / "preflight.json").write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2), "utf-8"
    )
    assert all(row["observation"]["facts"]["result"]["success"] for row in preflight), directory
    (ROOT / f"artifacts/{run_name}-current.txt").write_text(str(directory), "utf-8")
    if args.smoke:
        return
    positive = preflight[0]
    negative = {
        **positive["case"],
        "id": "contradictory-calibration-rubric",
        "rules": [{**rule, "expected": False} for rule in positive["case"]["rules"]],
    }
    annotations = [
        {
            "case": case,
            "observation": observation,
            "expected": {rule["dimension"]: label for rule in case["rules"]},
        }
        for case, observation, label in [
            (positive["case"], positive["observation"], "pass"),
            (negative, positive["observation"], "fail"),
            (positive["case"], None, "unverifiable"),
        ]
    ]
    calibration = calibrate_business(app, annotations, "native-boundary-rubric-v1", 1)
    groups = 8 if args.compare_tool_budget else 2
    cases = [case_for(scenario, index + 1, "train") for index, scenario in enumerate(scenarios)]
    cases += [
        case_for(
            scenarios[index % len(scenarios)]
            if args.compare_tool_budget or args.provider_retry
            else "legitimate"
            if args.kind == "nexrag"
            else "changed_data",
            base + index,
            split,
        )
        for base, split in ((100, "selection"), (200, "holdout"), (300, "counterexample"))
        for index in range(groups)
    ]
    proposals = (
        [
            Proposal(
                changes={"tool_budget": 4},
                source="config",
                expected_baseline=version,
                hypothesis="Allow native recovery and verification after the first tool attempt",
                risk="Repeated edits or a failed verification could consume the extra actions",
            )
        ]
        if args.compare_tool_budget
        else []
    )
    plan = Plan(
        application=app,
        dataset_version="native-fault-counterexamples-v1"
        if proposals
        else "native-boundary-generated-v1",
        calibration=Calibration.model_validate(calibration["calibration"]),
        cases=cases,
        epochs=1,
        objective="quality",
        minimum_gain=0.05,
        quality_tolerance=0,
        minimum_groups=groups,
        budget=Budget(application=0, grading=0, analysis=0, reflection=0),
        environment={
            "mode": "actual application boundaries with scripted dependencies",
            "reset": "fresh process and isolated application state each attempt",
            "scope": "paired tool budget" if proposals else "baseline boundary verification",
        },
    )
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "plan": plan.model_dump(mode="json"),
                "calibration": calibration,
                "calibration_mode": (
                    "native positive; contradictory negative rubric; missing observation"
                ),
                "use_gepa": False,
                "proposals": [proposal.model_dump(mode="json") for proposal in proposals],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )
    service = Service(directory / "workspace", background=False)
    try:
        service.register(app, key="app")
        service.store.update(
            lambda state: state["calibrations"].update(
                {calibration["calibration"]["artifact"]: calibration}
            )
        )
        created = service.create(plan, proposals, key="boundaries", use_gepa=False)
        service.start(created["id"], created["fingerprint"])
        service.run(created["id"])
        experiment = service.store.read()["experiments"][created["id"]]
        assert experiment["status"] == "completed", experiment.get("reason")
        trials = [trial for run in experiment["runs"].values() for trial in load_trials(run)]
        if not proposals:
            assert len(trials) == len(scenarios) + 2
            assert all(
                j.status in {"pass", "not_applicable"} for trial in trials for j in trial.judgments
            )
        else:
            assert all(
                j.status == "pass"
                for trial in trials
                for j in trial.judgments
                if j.dimension == Dimension.SECURITY
            )
        result = {
            "status": experiment["status"],
            "experiment_id": created["id"],
            "application_version": version,
            "rounds": len(trials),
            "scenarios": scenarios,
            "ledger": experiment["ledger"],
            "logs": sorted({trial.log for trial in trials}),
            "scope": (
                "Actual boundary/API/tool execution with generated data; "
                "no production or model-quality claim"
            ),
            "independent_reports": experiment["reports"],
        }
        (directory / "result.json").write_text(json.dumps(result, indent=2), "utf-8")
        print(json.dumps(result, indent=2))
        if proposals:
            assert len(experiment["reports"]) == 1
            assert next(iter(experiment["reports"].values()))["decision"] == "recommend"
    finally:
        service.close()


if __name__ == "__main__":
    main()
