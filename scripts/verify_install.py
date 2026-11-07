"""Run with the wheel-installed Python (-I) from any working directory."""

import json
import sys
from importlib import import_module
from importlib.metadata import version
from pathlib import Path

from evals_app.build import build_info
from evals_app.fixtures import fixture_plan
from evals_app.logs import load_trials
from evals_app.rescan import rescan
from evals_app.service import Service


def main():
    directory = Path(sys.argv[1]).resolve()
    imports = {}
    for module in ("evals_app", "inspect_ai", "inspect_scout", "gepa"):
        location = Path(import_module(module).__file__).resolve()
        if not location.is_relative_to(Path(sys.prefix).resolve()):
            raise RuntimeError(f"Import escaped the installation environment: {location}")
        imports[module] = str(location)
    # Namespace packages were previously lost during wheel discovery.
    for module in (
        "inspect_ai.util._sandbox.docker.docker",
        "inspect_ai._sentinel._agent_state",
        "inspect_ai._control.transport",
    ):
        import_module(module)
    plan = fixture_plan(groups=2)
    service = Service(directory, background=False)
    try:
        service.register(plan.application, key="app")
        created = service.create(plan, [], key="installed", use_gepa=False)
        service.start(created["id"], created["fingerprint"])
        service.run(created["id"])
        experiment = service.store.read()["experiments"][created["id"]]
        assert experiment["status"] == "completed", experiment.get("reason")
        rounds = sum(len(load_trials(run)) for run in experiment["runs"].values())
        scan = rescan(service, created["id"])
        assert scan["complete"] and not scan["error_count"]
        rows = json.loads(scan["results_json"]["evals_business_findings"])
        assert len(rows) == rounds
        assert all(
            any(f["path"] == "process.calls.1" for f in json.loads(row["explanation"]))
            for row in rows
        )
        result = {
            "imports": imports,
            "build": build_info(),
            "status": experiment["status"],
            "rounds": rounds,
            "scout": {
                **{k: scan[k] for k in ("complete", "error_count", "application_calls")},
                "findings_verified": len(rows),
            },
            "versions": {
                name: version(name) for name in ("evals-app", "inspect-ai", "inspect-scout", "gepa")
            },
        }
        (directory / "installation.json").write_text(json.dumps(result, indent=2), "utf-8")
        print(json.dumps(result, indent=2))
    finally:
        service.close()


if __name__ == "__main__":
    main()
