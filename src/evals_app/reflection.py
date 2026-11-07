"""Isolated reflection plugin protocol; only approved development feedback enters."""

import inspect
import json
import sys
from pathlib import Path

import anyio

from evals_app.adapters import load_function


def main() -> None:
    path = Path(sys.argv[1])
    job = json.loads(path.read_text("utf-8"))
    function = load_function(job["entrypoint"])
    try:
        result = (
            anyio.run(function, job["payload"])
            if inspect.iscoroutinefunction(function)
            else function(job["payload"])
        )
        path.with_suffix(".result.json").write_text(json.dumps(result, allow_nan=False), "utf-8")
    except Exception as error:
        path.with_suffix(".result.json").write_text(json.dumps({"error": str(error)}), "utf-8")
        raise


if __name__ == "__main__":
    main()
