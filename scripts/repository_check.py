"""Check the proposed repository contents without printing any secret values."""

import hashlib
import math
import re
import subprocess
from collections import Counter
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    names = sorted(set(result.stdout.decode("utf-8").split("\0")) - {""})
    failures = []
    pattern = re.compile(
        rb"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{28,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"
    )
    for name in names:
        path = root / name
        if not path.is_file():
            continue
        if any(
            part in {"node_modules", ".venv", "__pycache__", "artifacts", ".bug-hunter"}
            for part in path.relative_to(root).parts
        ):
            failures.append(f"Generated or private file included: {name}")
        if path.stat().st_size > 50_000_000:
            failures.append(f"Oversized file: {name}")
        data = path.read_bytes()
        for match in pattern.findall(data):
            # Fixed local-proxy test key from the pinned upstream; never a provider credential.
            if (
                name == "engine/inspect_ai/tests/test_helpers/litellm_proxy/proxy.py"
                and hashlib.sha256(match).hexdigest()
                == "b7b3f7b7404157e9304c76b1a61a1fa77883af61e0f634649ae1277ac33d1f2e"
            ):
                continue
            frequencies = Counter(match)
            entropy = -sum(
                (count / len(match)) * math.log2(count / len(match))
                for count in frequencies.values()
            )
            if entropy > 3.5:
                failures.append(f"Potential credential: {name}")
                break
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Repository gate passed for {len(names)} files; no credential values printed.")


if __name__ == "__main__":
    main()
