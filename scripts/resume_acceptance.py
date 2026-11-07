"""Explicitly resume an interrupted, unbilled isolated acceptance workspace."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

import psutil
from acceptance_applications import application_server

from evals_app.models import Plan, identifier, now
from evals_app.service import Service


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["vera", "nexrag"], required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--acceptance", type=Path, required=True)
    args = parser.parse_args()
    directory = args.acceptance.resolve()
    state = json.loads((directory / "workspace/state.json").read_text("utf-8"))
    original = next(
        e for e in state["experiments"].values() if e["status"] not in {"completed", "cancelled"}
    )
    plan = Plan.model_validate(original["plan"])
    if any(plan.budget.model_dump().values()) or plan.application.estimate_per_attempt:
        raise ValueError(
            "This recovery helper only reconciles known unbilled deterministic fixtures"
        )
    if state["operations"]:
        raise ValueError("An effective version operation requires its own recovery procedure")
    for process in psutil.process_iter(["cmdline"]):
        command = " ".join(process.info["cmdline"] or [])
        if (
            process.name().lower().startswith("python")
            and "serve_local.py" in command
            and str(directory) in command
        ):
            raise RuntimeError(
                "Original isolated application is still alive; verify it before recovery"
            )
    resumed = directory / identifier("resumed")
    resumed.mkdir()
    with application_server(
        args.kind, args.source.resolve(), resumed, port=urlparse(plan.application.entrypoint).port
    ) as (url, version, client, token):
        effective = client.get(url + "/effective", headers={"Authorization": f"Bearer {token}"})
        effective.raise_for_status()
        if (
            version != plan.application.version
            or effective.json()["config"] != plan.application.config
        ):
            raise RuntimeError(
                "Frozen application baseline differs from the reset isolated environment"
            )
        service = Service(directory / "workspace", background=False)
        try:
            experiment = service.store.read()["experiments"][original["id"]]
            evidence = {
                "source": "isolated-acceptance-recovery",
                "locator": str(resumed),
                "collected_at": now(),
                "basis": (
                    "Owned worker exited; fresh sandbox, exact baseline/configuration; "
                    "deterministic dependencies have no billed provider calls"
                ),
            }
            (resumed / "recovery.json").write_text(json.dumps(evidence, indent=2), "utf-8")
            costs = {
                key: 0.0
                for key, r in experiment["reservations"].items()
                if r["status"] == "unknown"
            }
            service.recover(original["id"], experiment["fingerprint"], costs, evidence)
            service.run(original["id"])
            experiment = service.store.read()["experiments"][original["id"]]
            result = {
                "directory": str(directory),
                "application": plan.application.model_dump(mode="json"),
                "status": experiment["status"],
                "reason": experiment.get("reason"),
                "decisions": {k: r["decision"] for k, r in experiment["reports"].items()},
                "resumed_from": str(resumed),
                "fixture_data": True,
                "boundary": (
                    "Real isolated application code with deterministic dependencies; "
                    "no production quality or sustained benefit claim"
                ),
            }
            (directory / "result.json").write_text(json.dumps(result, indent=2), "utf-8")
            print(json.dumps(result, indent=2))
            if result["status"] != "completed":
                raise RuntimeError("Recovery did not complete; preserved the experiment state")
        finally:
            service.close()


if __name__ == "__main__":
    main()
