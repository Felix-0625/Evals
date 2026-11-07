"""Identify the actual application adapter and maintained kernel integration code."""

import hashlib
from importlib.metadata import version
from pathlib import Path

import inspect_ai


def build_info() -> dict[str, str]:
    product = Path(__file__).parent
    kernel = Path(inspect_ai.__file__).parent
    digest = hashlib.sha256()
    files = [(f"evals/{p.name}", p) for p in sorted(product.glob("*.py"))]
    files += [
        (f"kernel/{name}", kernel / name)
        for name in (
            "_control/server.py",
            "_control/discovery.py",
            "_control/transport.py",
            "_cli/ctl/_http.py",
            "_view/fastapi_server.py",
            "_eval/eval.py",
            "_eval/evalset.py",
            "_eval/task/scan.py",
            "_util/logger.py",
            "_util/trace.py",
            "model/_providers/ollama.py",
        )
    ]
    for name, path in files:
        digest.update(name.encode())
        digest.update(path.read_bytes())
    return {
        "evals_version": version("evals-app"),
        "kernel_version": version("inspect-ai"),
        "scout_version": version("inspect-scout"),
        "gepa_version": version("gepa"),
        "integration_fingerprint": digest.hexdigest(),
        "kernel_upstream": "8cef181c1f04d3fae70db95c6e7bef5715df7260",
    }
