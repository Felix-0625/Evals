"""Product entrypoint using the same service as HTTP and the Viewer."""

import argparse
import json
from pathlib import Path
from typing import Literal

import uvicorn

from evals_app.api import CreateRequest, create_app
from evals_app.calibration import calibrate
from evals_app.fixtures import fixture_plan
from evals_app.models import Proposal
from evals_app.service import Service


def demo(directory: Path, kind: Literal["harness", "rag"] = "rag", use_gepa: bool = True) -> dict:
    service = Service(directory, background=False)
    plan = fixture_plan(kind)
    try:
        service.register(plan.application, key=f"demo-app-{kind}")
        specifications: list[tuple[dict[str, str | int | float | bool], str, str]] = [
            ({"reuse": True}, "Reuse identical tool results", "Identity/data changes"),
            ({"reuse": True, "prompt": "grounded"}, "Grounding plus reuse", "Prompt regression"),
            (
                {"reuse": True, "policy": "unsafe"},
                "Adversarial unsafe shortcut",
                "Unauthorized access",
            ),
            ({"prompt": "grounded"}, "Grounding without cost benefit", "No primary objective gain"),
        ]
        proposals = [
            Proposal(
                changes=changes,
                source="manual",
                hypothesis=hypothesis,
                risk=risk,
                expected_baseline=plan.application.version,
            )
            for changes, hypothesis, risk in specifications
        ]
        created = service.create(plan, proposals, key=f"demo-experiment-{kind}", use_gepa=use_gepa)
        experiment = service.store.read()["experiments"][created["id"]]
        if experiment["status"] == "draft":
            service.start(created["id"], created["fingerprint"])
            service.run(created["id"])
        return service.store.read()["experiments"][created["id"]]
    finally:
        service.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="evals")
    parser.add_argument("--workspace", type=Path, default=Path("evals-workspace"))
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=7575)
    sample = commands.add_parser("demo")
    sample.add_argument("--kind", choices=["harness", "rag"], default="rag")
    sample.add_argument("--no-gepa", action="store_true")
    run = commands.add_parser("run")
    run.add_argument("manifest", type=Path)
    scan = commands.add_parser("rescan")
    scan.add_argument("experiment_id")
    commands.add_parser("status")
    commands.add_parser("calibrate")
    args = parser.parse_args()
    if args.command == "serve":
        app = create_app(args.workspace, port=args.port)
        print(f"Evals: http://127.0.0.1:{args.port}/#/evals")
        uvicorn.run(app, host="127.0.0.1", port=args.port)
    elif args.command == "calibrate":
        print(json.dumps(calibrate(), indent=2))
    elif args.command == "demo":
        result = demo(args.workspace, args.kind, not args.no_gepa)
        print(
            json.dumps(
                {
                    k: result.get(k)
                    for k in ("id", "status", "reason", "conclusion", "reports", "ledger")
                },
                indent=2,
            )
        )
        if result["status"] != "completed":
            raise SystemExit(1)
    else:
        service = Service(args.workspace, background=False)
        try:
            if args.command == "rescan":
                from evals_app.rescan import rescan

                print(json.dumps(rescan(service, args.experiment_id), ensure_ascii=False, indent=2))
            elif args.command == "status":
                print(json.dumps(service.store.read(), ensure_ascii=False, indent=2))
            else:
                body = CreateRequest.model_validate_json(args.manifest.read_text("utf-8"))
                service.register(body.plan.application, key=f"manifest:{body.plan.application.id}")
                created = service.create(
                    body.plan,
                    body.proposals,
                    key=str(args.manifest.resolve()),
                    use_gepa=body.use_gepa,
                    policy=body.policy,
                )
                service.start(created["id"], created["fingerprint"])
                service.run(created["id"])
                result = service.store.read()["experiments"][created["id"]]
                print(json.dumps(result, ensure_ascii=False, indent=2))
                if result["status"] != "completed":
                    raise SystemExit(1)
        finally:
            service.close()


if __name__ == "__main__":
    main()
