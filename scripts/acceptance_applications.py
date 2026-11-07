"""Run reproducible external-application acceptance in disposable local workspaces.

The application code is real. Model replies and query embeddings are deterministic
test dependencies, and the report states this boundary explicitly.
"""

import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import psutil

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
from evals_app.rescan import rescan
from evals_app.service import Service
from inspect_scout import scan_results_df

ROOT = Path(__file__).resolve().parents[1]
TARGETED_SCENARIOS = ("identity_switch", "data_update", "long_tail_join", "long_tail_multihop")


@contextmanager
def application_server(kind: str, source: Path, directory: Path, *, port: int | None = None):
    if port is None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
    python = source / (
        "vera-agent/.venv/Scripts/python.exe"
        if kind == "vera"
        else "base/retrieval-agent/.venv/Scripts/python.exe"
    )
    env = dict(os.environ, EVALS_ISOLATED_TOKEN=secrets.token_hex(32), PYTHONIOENCODING="utf-8")
    previous = os.environ.get("EVALS_ISOLATED_TOKEN")
    os.environ["EVALS_ISOLATED_TOKEN"] = env["EVALS_ISOLATED_TOKEN"]
    with (directory / f"{kind}-server.txt").open("wb") as output:
        process = subprocess.Popen(
            [
                str(python),
                "-B",
                str(ROOT / "examples/integrations/serve_local.py"),
                "--kind",
                kind,
                "--source",
                str(source),
                "--artifacts",
                str(directory / "native"),
                "--port",
                str(port),
            ],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 90
            with httpx.Client(timeout=90, trust_env=False) as client:
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(
                            f"{kind} server exited; inspect {directory / f'{kind}-server.txt'}"
                        )
                    try:
                        response = client.get(url)
                        response.raise_for_status()
                        break
                    except httpx.TransportError as error:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"{kind} local server did not become ready"
                            ) from error
                        time.sleep(0.2)
                yield url, response.json()["version"], client, env["EVALS_ISOLATED_TOKEN"]
        finally:
            try:
                children = psutil.Process(process.pid).children(recursive=True)
            except psutil.NoSuchProcess:
                children = []
            for child in reversed(children):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if previous is None:
                os.environ.pop("EVALS_ISOLATED_TOKEN", None)
            else:
                os.environ["EVALS_ISOLATED_TOKEN"] = previous


def application(kind: str, url: str, version: str) -> Application:
    assets = (
        {
            "tool_budget": Asset(kind="choice", values=[1, 4]),
            "prompt_rule": Asset(kind="text", values=["", "Run acceptance before finishing."]),
        }
        if kind == "vera"
        else {
            "top_k": Asset(kind="choice", values=[1, 2]),
            "hybrid": Asset(kind="choice", values=[False, True]),
        }
    )
    config = (
        {"tool_budget": 1, "prompt_rule": ""} if kind == "vera" else {"top_k": 1, "hybrid": False}
    )
    return Application(
        id=f"isolated-{kind}",
        version=version,
        kind="harness" if kind == "vera" else "rag",
        adapter="http",
        entrypoint=url,
        identity_ref="EVALS_ISOLATED_TOKEN",
        provenance="real",
        executor="local",
        data_source="Generated isolated addition tasks"
        if kind == "vera"
        else "Generated tenant-scoped Qdrant documents",
        observable=list(Dimension),
        assets=assets,
        config=config,
        timeout=90,
        evidence_scope="shareable",
        estimate_per_attempt=0,
    )


def cases_for(kind: str, groups: int) -> list[Case]:
    cases = []
    for split_index, split in enumerate(("train", "selection", "holdout", "counterexample")):
        for index in range(groups):
            paths = [
                (Dimension.RESULT, "result.success", True),
                (Dimension.SECURITY, "security.authorized", True),
                (Dimension.EVIDENCE, "evidence.supported", True),
                (Dimension.COST, "cost.known", True),
            ]
            if kind == "vera":
                paths.append((Dimension.PROCESS, "process.valid", True))
            else:
                paths.append((Dimension.RETRIEVAL, "retrieval.context", ["policy", "expiry"]))
                paths.append((Dimension.RECOVERY, "recovery.consistent", True))
            rules = [
                Rule(
                    dimension=dimension,
                    path=path,
                    expected=expected,
                    operator="subset" if dimension == Dimension.RETRIEVAL else "eq",
                    critical=dimension == Dimension.SECURITY,
                    source="isolated-application-verifier",
                )
                for dimension, path, expected in paths
            ]
            number = split_index * 100 + index + 1
            cases.append(
                Case(
                    id=f"{kind}-{split}-{index}",
                    group=f"{kind}-{split}-session-{index}",
                    split=split,
                    input=f"{kind} isolated task {number} in partition {split}",
                    rules=rules,
                    metadata={"number": number, "attack": kind == "vera" and index % 4 == 0},
                )
            )
    return cases


def request_observation(client, app, case, token, config=None):
    response = client.post(
        app.entrypoint,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "input": case.input,
            "config": config or app.config,
            "metadata": case.metadata,
            "context": {"sample_id": case.id},
            "application_version": app.version,
        },
    )
    response.raise_for_status()
    return response.json()


def targeted_case(case: Case, scenario: str) -> Case:
    rules = case.rules
    if scenario == "long_tail_multihop":
        rules = [
            rule.model_copy(update={"expected": ["policy", "expiry", "condition"]})
            if rule.dimension == Dimension.RETRIEVAL
            else rule
            for rule in rules
        ]
    return case.model_copy(
        update={
            "input": f"{case.input} / {scenario}",
            "metadata": {**case.metadata, "scenario": scenario},
            "rules": rules,
        }
    )


def verify_rerank_stages(service: Service, experiment: dict, directory: Path) -> dict:
    selected = experiment["selected_candidate"]
    assert experiment["reports"][selected]["decision"] == "recommend"
    findings = experiment["findings"]
    assert len(findings) == 4 and all(
        f["stage"] == "rerank" and f["document_ids"] == ["expiry"] for f in findings
    )
    pairs = {}
    for arm, expected, status in (
        ("baseline", ["policy"], "fail"),
        (selected, ["policy", "expiry"], "pass"),
    ):
        rows = load_trials(experiment["runs"][f"{arm}:counterexample"])
        assert len(rows) == 4
        pairs[arm] = []
        for trial in rows:
            assert trial.observation is not None
            stages = trial.observation.facts["retrieval"]
            assert stages["recalled"] == ["policy", "expiry"]
            assert stages["reranked"] == stages["context"] == expected
            outcomes = {j.dimension.value: j.status for j in trial.judgments}
            assert outcomes["result"] == outcomes["retrieval"] == status
            assert outcomes["security"] == "pass"
            pairs[arm].append(
                {
                    "sample": trial.sample_id,
                    "epoch": trial.epoch,
                    "stages": stages,
                    "result": outcomes["result"],
                    "log": trial.log,
                    "evidence": trial.observation.evidence["retrieval.reranked"].locator,
                }
            )
    scan = rescan(service, experiment["id"])
    assert scan["complete"] and scan["error_count"] == scan["application_calls"] == 0
    scanner_rows = json.loads(scan["results_json"]["evals_business_findings"])
    online_rows = []
    for log in scan["logs"]:
        (scan_path,) = (Path(log).parent / "scans").glob("scan_id=*")
        online_rows.extend(
            scan_results_df(str(scan_path))
            .scanners["evals_business_findings"]
            .to_dict(orient="records")
        )

    def by_sample(rows):
        return {
            (
                Path(row["transcript_source_uri"]).name,
                row["transcript_task_id"],
                row["transcript_task_repeat"],
            ): json.loads(row["explanation"])
            for row in rows
        }

    assert len(online_rows) == len(scanner_rows) == len(by_sample(scanner_rows)) == 12
    assert by_sample(online_rows) == by_sample(scanner_rows)
    detected = [finding for row in scanner_rows for finding in json.loads(row["explanation"])]
    assert len(detected) == 8 and all(
        f["stage"] == "rerank" and f["document_ids"] == ["expiry"] for f in detected
    )
    (directory / "scout-rescan.json").write_text(json.dumps(scan, indent=2), "utf-8")
    return {
        "rounds": sum(len(r["trial_index"]) for r in experiment["runs"].values()),
        "scout_findings": len(detected),
        "scout_online_offline_match": True,
        "scout_application_calls": scan["application_calls"],
        "counterexamples": pairs,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["vera", "nexrag"], required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--targeted-counterexamples", action="store_true")
    modes.add_argument("--rerank-stages", action="store_true")
    args = parser.parse_args()
    if args.targeted_counterexamples and args.kind != "nexrag":
        parser.error("--targeted-counterexamples requires --kind nexrag")
    if args.rerank_stages and args.kind != "nexrag":
        parser.error("--rerank-stages requires --kind nexrag")
    if args.rerank_stages and subprocess.check_output(
        ["git", "-C", str(args.source), "status", "--porcelain", "--", "base/retrieval-agent"]
    ):
        raise ValueError("The frozen NexRAG retrieval component has worktree changes")
    directory = ROOT / "artifacts" / identifier(f"applications-{args.kind}")
    directory.mkdir(parents=True)
    with application_server(args.kind, args.source.resolve(), directory) as (
        url,
        version,
        client,
        token,
    ):
        app = application(args.kind, url, version)
        groups = 2 if args.rerank_stages else 8
        cases = cases_for(args.kind, groups)
        if args.rerank_stages:
            app = Application.model_validate(
                {
                    **app.model_dump(mode="json"),
                    "config": {"top_k": 2, "hybrid": False, "rerank_min_score": 0.50},
                    "assets": {
                        "top_k": Asset(kind="choice", values=[2]),
                        "hybrid": Asset(kind="choice", values=[False]),
                        "rerank_min_score": Asset(kind="choice", values=[0.50, 0.10]),
                    },
                }
            )
            cases = [targeted_case(case, "rerank_loss") for case in cases]
        calibration_examples = [cases[1]]
        if args.targeted_counterexamples:
            calibration_examples.append(targeted_case(cases[1], "long_tail_multihop"))
            cases = [
                targeted_case(case, TARGETED_SCENARIOS[index % len(TARGETED_SCENARIOS)])
                if case.split == "counterexample"
                else case
                for index, case in enumerate(cases)
            ]
        good_config = (
            {**app.config, "tool_budget": 4} if args.kind == "vera" else {**app.config, "top_k": 2}
        )
        if args.rerank_stages:
            good_config = {**app.config, "rerank_min_score": 0.10}
        annotations = []
        failure_dimensions = {"result"} if args.kind == "vera" else {"result", "retrieval"}
        for number, case in enumerate(calibration_examples, 10001):
            calibration_case = case.model_copy(
                update={
                    "id": f"{args.kind}-calibration-{number}",
                    "group": f"calibration-only-{number}",
                    "input": f"Independent calibration task {number}",
                    "metadata": {**case.metadata, "number": number},
                }
            )
            positive = request_observation(client, app, calibration_case, token, good_config)
            negative = request_observation(client, app, calibration_case, token)
            for value, label in ((positive, "pass"), (negative, "fail"), (None, "unverifiable")):
                expected = {rule.dimension.value: label for rule in calibration_case.rules}
                if label == "fail":
                    expected = {
                        dimension: "fail" if dimension in failure_dimensions else "pass"
                        for dimension in expected
                    }
                annotations.append(
                    {
                        "case": calibration_case.model_dump(mode="json"),
                        "observation": value,
                        "expected": expected,
                    }
                )
        calibration_version = (
            "isolated-rerank-calibration-v1"
            if args.rerank_stages
            else "isolated-targeted-calibration-v2"
            if args.targeted_counterexamples
            else "isolated-calibration-v1"
        )
        result = calibrate_business(app, annotations, calibration_version, 1)
        (directory / "calibration.json").write_text(json.dumps(result, indent=2), "utf-8")
        if args.smoke:
            print(
                json.dumps(
                    {
                        "directory": str(directory),
                        "calibration": result,
                        "positive": positive["facts"]["result"],
                        "negative": negative["facts"]["result"],
                    },
                    indent=2,
                )
            )
            return
        service = Service(directory / "workspace", background=False)
        try:
            service.register(app, key="application")
            service.store.update(
                lambda state: state["calibrations"].update(
                    {result["calibration"]["artifact"]: result}
                )
            )
            plan = Plan(
                application=app,
                dataset_version="isolated-rerank-v1"
                if args.rerank_stages
                else "isolated-targeted-v2"
                if args.targeted_counterexamples
                else "isolated-generated-v1",
                calibration=Calibration.model_validate(result["calibration"]),
                cases=cases,
                objective="quality",
                minimum_gain=0.05,
                quality_tolerance=0,
                minimum_groups=groups,
                epochs=2,
                budget=Budget(application=0, grading=0, analysis=0, reflection=0),
                environment={
                    "mode": "real application with deterministic dependencies",
                    "reset": "fresh sandbox or Qdrant memory collection per call",
                    "model": (
                        "fixed query vectors and scripted cross-encoder scores"
                        if args.rerank_stages
                        else "scripted replies"
                        if args.kind == "vera"
                        else "fixed query vectors"
                    ),
                    "cache": "reset each request",
                    "gateway_sha256": hashlib.sha256(
                        (ROOT / "examples/integrations/serve_local.py").read_bytes()
                    ).hexdigest(),
                },
            )
            variants = (
                [{"tool_budget": 4}, {"prompt_rule": "Run acceptance before finishing."}]
                if args.kind == "vera"
                else [{"top_k": 2}, {"hybrid": True}]
            )
            if args.rerank_stages:
                variants = [{"rerank_min_score": 0.10}]
            proposals = [
                Proposal(
                    changes=changes,
                    source="config" if index == 0 else "manual",
                    hypothesis="Increase execution evidence coverage"
                    if index == 0
                    else "Independent alternative control",
                    risk="Overhead or unrelated regression",
                    expected_baseline=version,
                )
                for index, changes in enumerate(variants)
            ]
            created = service.create(
                plan, proposals, key="experiment", use_gepa=args.kind == "vera"
            )
            (directory / "manifest.json").write_text(
                json.dumps(
                    {
                        "plan": plan.model_dump(mode="json"),
                        "proposals": [p.model_dump(mode="json") for p in proposals],
                    },
                    indent=2,
                ),
                "utf-8",
            )
            service.start(created["id"], created["fingerprint"])
            service.run(created["id"])
            experiment = service.store.read()["experiments"][created["id"]]
            decisions = {
                candidate: report["decision"] for candidate, report in experiment["reports"].items()
            }
            report = {
                "directory": str(directory),
                "application": app.model_dump(mode="json"),
                "status": experiment["status"],
                "experiment_id": created["id"],
                "reason": experiment.get("reason"),
                "decisions": decisions,
                "fixture_data": True,
                "boundary": (
                    "Real application execution with deterministic model/embedding; "
                    "no live-model or production benefit claim"
                ),
            }
            if args.targeted_counterexamples:
                selected = next(
                    (key for key, value in decisions.items() if value == "recommend"), None
                )
                pairs = {}
                for arm in ("baseline", selected) if selected else ("baseline",):
                    run = experiment["runs"].get(f"{arm}:counterexample")
                    if run is None:
                        continue
                    pairs[arm] = [
                        {
                            "sample_id": trial.sample_id,
                            "epoch": trial.epoch,
                            "log": trial.log,
                            "scenario": trial.observation.facts["native"]["scenario"],
                            "data_verified": trial.observation.facts["native"][
                                "required_data_verified"
                            ],
                            "dependent_hop_verified": trial.observation.facts["native"].get(
                                "dependent_hop_verified"
                            ),
                            "security": next(
                                j.status
                                for j in trial.judgments
                                if j.dimension == Dimension.SECURITY
                            ),
                            "result": next(
                                j.status for j in trial.judgments if j.dimension == Dimension.RESULT
                            ),
                        }
                        for trial in load_trials(run)
                        if trial.observation
                    ]
                report["targeted_counterexamples"] = pairs
                if selected:
                    assert experiment["candidates"][selected]["config"]["top_k"] == 2
                    assert len(pairs[selected]) == 16
                    assert {row["scenario"] for row in pairs[selected]} == set(TARGETED_SCENARIOS)
                    assert all(
                        row["data_verified"] and row["security"] == row["result"] == "pass"
                        for row in pairs[selected]
                    )
                    for arm, outcome in (("baseline", "fail"), (selected, "pass")):
                        hops = [
                            row for row in pairs[arm] if row["scenario"] == "long_tail_multihop"
                        ]
                        assert len(hops) == 4
                        assert all(
                            row["dependent_hop_verified"]
                            and row["security"] == "pass"
                            and row["result"] == outcome
                            for row in hops
                        )
            if args.rerank_stages:
                report["rerank_stages"] = verify_rerank_stages(service, experiment, directory)
                report["boundary"] = (
                    "Native NexRAG search and Qdrant with scripted vectors and cross-encoder scores"
                )
            (directory / "result.json").write_text(json.dumps(report, indent=2), "utf-8")
            print(json.dumps(report, indent=2))
            if experiment["status"] != "completed" or "recommend" not in decisions.values():
                raise RuntimeError(
                    "Application acceptance did not complete "
                    "with an independently verified candidate"
                )
        finally:
            service.close()


if __name__ == "__main__":
    main()
