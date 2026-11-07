"""Controlled native solver fault for acceptance; never used by product dispatch."""

import sys
import time
from pathlib import Path

import anyio
from inspect_ai.solver import solver

from evals_app import worker


def main():
    original = worker.application_solver
    marker, held, release = (Path(value) for value in sys.argv[2:])

    @solver
    def transient_solver(*args, **kwargs):
        solve = original(*args, **kwargs)

        async def run(state, generate):
            if str(state.sample_id) == "train-0" and not marker.exists():
                marker.write_text("injected native infrastructure failure", "utf-8")
                raise RuntimeError("EVALS_ACCEPTANCE_TRANSIENT_SOLVER_FAILURE")
            if str(state.sample_id) == "train-1":
                held.write_text("other sample keeps the native run active", "utf-8")
                deadline = time.monotonic() + 75
                while not release.exists():
                    if time.monotonic() > deadline:
                        raise TimeoutError("Acceptance driver did not release the held sample")
                    await anyio.sleep(0.05)
            return await solve(state, generate)

        return run

    worker.application_solver = transient_solver
    worker.main()


if __name__ == "__main__":
    main()
