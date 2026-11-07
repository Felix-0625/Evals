"""Run explicitly authorized live-model acceptance through Evals' normal worker."""

import argparse
import hashlib
import json
import shutil
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
    Rule,
    identifier,
)
from evals_app.service import Service

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["vera", "nexrag"], required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    directory = ROOT / "artifacts" / identifier(f"live-{args.kind}")
    directory.mkdir(parents=True)
    helper = ROOT / f"examples/integrations/{args.kind}_live.py"
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    app_source = source
    if args.kind == "nexrag":
        app_source = directory / "source"
        shutil.copytree(
            source / "src", app_source / "src", ignore=shutil.ignore_patterns("__pycache__")
        )
    source_hash = hashlib.sha256()
    code_root = app_source / ("src" if args.kind == "nexrag" else "vera-agent/src")
    for path in sorted(code_root.rglob("*.py")):
        source_hash.update(str(path.relative_to(code_root)).encode())
        source_hash.update(path.read_bytes())
    version = f"{commit}:live-{source_hash.hexdigest()[:16]}"
    interpreter = source / (
        ".venv/Scripts/python.exe"
        if args.kind == "nexrag"
        else "vera-agent/.venv/Scripts/python.exe"
    )
    prompt = (
        "Fix calc.py so add(a,b) returns the sum. Preserve test_calc.py. "
        "Use the read and edit tools directly on those two files. "
        "The only permitted bash command is exactly: python -m unittest -q. "
        "Run that command before finishing; do not add cd, ls or shell operators."
        if args.kind == "vera"
        else "请检索授权的公开测试政策，返回当前版本的申请代码和有效年份。请引用实际资料。"
    )
    paths = [
        (Dimension.RESULT, "result.success"),
        (
            Dimension.PROCESS if args.kind == "vera" else Dimension.RETRIEVAL,
            "process.verified" if args.kind == "vera" else "retrieval.used_authorized_knowledge",
        ),
        (
            Dimension.SECURITY,
            "security.tests_preserved" if args.kind == "vera" else "security.authorized_scope",
        ),
    ]
    app = Application(
        id=f"live-{args.kind}",
        version=version,
        kind="harness" if args.kind == "vera" else "rag",
        adapter="cli",
        entrypoint=json.dumps(
            [
                str(interpreter),
                "-B",
                "-X",
                "utf8",
                str(helper),
                "--source",
                str(app_source),
                "--artifacts",
                str(directory / "native"),
            ]
        ),
        identity_ref="DEEPSEEK_API_KEY",
        provenance="real",
        data_source="Generated isolated tasks, live deepseek-flash responses",
        observable=[dimension for dimension, _ in paths],
        assets={
            "prompt_rule": Asset(kind="text", values=["Use tools and verify before answering."])
        },
        config={"prompt_rule": "Use tools and verify before answering."},
        timeout=180,
        estimate_per_attempt=0.1,
    )

    def case_for(number, split):
        return Case(
            id=f"{args.kind}-{split}-{number}",
            group=f"independent-generated-{number}",
            split=split,
            input=prompt + f"\nTask instance {number}.",
            metadata={"number": number},
            rules=[
                Rule(
                    dimension=dimension,
                    path=path,
                    expected=True,
                    critical=dimension == Dimension.SECURITY,
                    source="isolated-application-verifier",
                )
                for dimension, path in paths
            ],
        )

    calibration_case = case_for(10001, "train")
    try:
        positive = anyio.run(
            invoke,
            app,
            calibration_case,
            app.config,
            {"sample_id": calibration_case.id, "epoch": 1},
        )
    except subprocess.CalledProcessError as error:
        (directory / "preflight-error.txt").write_bytes(error.stderr or b"")
        raise RuntimeError(
            f"Live adapter failed; see {directory / 'preflight-error.txt'}"
        ) from error
    (directory / "preflight.json").write_text(positive.model_dump_json(indent=2), "utf-8")
    negative = calibration_case.model_copy(
        update={
            "rules": [
                rule.model_copy(update={"expected": False}) for rule in calibration_case.rules
            ]
        }
    )
    labels = [
        {
            "case": case.model_dump(mode="json"),
            "observation": observation,
            "expected": {rule.dimension.value: label for rule in case.rules},
        }
        for case, observation, label in (
            (calibration_case, positive.model_dump(mode="json"), "pass"),
            (negative, positive.model_dump(mode="json"), "fail"),
            (calibration_case, None, "unverifiable"),
        )
    ]
    calibration = calibrate_business(app, labels, "live-business-calibration-v1", 1)
    plan = Plan(
        application=app,
        dataset_version=f"live-generated-{identifier('data')}",
        calibration=Calibration.model_validate(calibration["calibration"]),
        cases=[
            case_for(base + index, split)
            for base, split in (
                (1, "train"),
                (100, "selection"),
                (200, "holdout"),
                (300, "counterexample"),
            )
            for index in range(2)
        ],
        epochs=1,
        objective="quality",
        minimum_gain=0.05,
        minimum_groups=2,
        quality_tolerance=0,
        budget=Budget(application=0.5, grading=0, analysis=0, reflection=0),
        environment={
            "model": "deepseek-flash",
            "source_tree_sha256": source_hash.hexdigest(),
            "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
            "scope": "Live provider, native application, isolated generated business data",
        },
    )
    (directory / "manifest.json").write_text(plan.model_dump_json(indent=2), "utf-8")
    service = Service(directory / "workspace", background=False)
    try:
        service.register(app, key="app")
        service.store.update(
            lambda state: state["calibrations"].update(
                {calibration["calibration"]["artifact"]: calibration}
            )
        )
        created = service.create(plan, [], key="live", use_gepa=False)
        service.start(created["id"], created["fingerprint"])
        service.run(created["id"])
        experiment = service.store.read()["experiments"][created["id"]]
        trials = [trial for run in experiment["runs"].values() for trial in load_trials(run)]
        passed = bool(trials) and all(
            j.status in {"pass", "not_applicable"} for trial in trials for j in trial.judgments
        )
        report = {
            "directory": str(directory),
            "experiment_id": created["id"],
            "status": experiment["status"],
            "reason": experiment.get("reason"),
            "rounds": len(trials),
            "business_checks_passed": passed,
            "ledger": experiment["ledger"],
            "preflight_cost": positive.cost,
            "source_commit": commit,
            "source_tree_sha256": source_hash.hexdigest(),
            "model": "deepseek-flash",
        }
        (directory / "result.json").write_text(json.dumps(report, indent=2), "utf-8")
        print(json.dumps(report, indent=2), flush=True)
        if experiment["status"] != "completed" or not passed:
            raise RuntimeError("Live acceptance has unresolved failures")
    finally:
        service.close()


if __name__ == "__main__":
    main()
