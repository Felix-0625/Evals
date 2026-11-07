"""M0/M1 installation and isolated Inspect/Scout smoke run."""

import json
from pathlib import Path

from evals_app.fixtures import fixture_plan
from evals_app.service import Service


def main():
    service = Service(Path("artifacts/kernel-smoke"), background=False)
    plan = fixture_plan(groups=2)
    try:
        service.register(plan.application, key="app")
        created = service.create(plan, [], key="experiment", use_gepa=False)
        service.start(created["id"], created["fingerprint"])
        trials = service._evaluate(
            created["id"], plan, {"id": "baseline", "config": plan.application.config}, "train"
        )
        print(
            json.dumps(
                {
                    "samples": len(trials),
                    "log": trials[0].log,
                    "judgments": [j.model_dump(mode="json") for j in trials[0].judgments],
                },
                indent=2,
            )
        )
    finally:
        service.close()


if __name__ == "__main__":
    main()
