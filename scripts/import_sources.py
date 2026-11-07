"""Import the recorded upstream trees without nested Git repositories."""

import io
import json
import re
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def export(source: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=True)
    data = subprocess.check_output(["git", "-C", str(source), "archive", "HEAD"])
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(destination, filter="data")


def main() -> None:
    manifest = json.loads((ROOT / "upstream-sources.json").read_text("utf-8"))
    targets = {
        "inspect_ai": "engine/inspect_ai",
        "inspect_scout": "components/inspect_scout",
        "gepa": "components/gepa",
    }
    for entry in manifest["sources"]:
        source = ROOT / entry["directory"]
        actual = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        if actual != entry["commit"]:
            raise ValueError(f"Unexpected source revision: {source}")
        export(source, ROOT / targets[entry["directory"]])
    frontend = Path("src/inspect_ai/_view/ts-mono")
    frontend_source = ROOT / "inspect_ai" / frontend
    actual = subprocess.check_output(
        ["git", "-C", str(frontend_source), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != "91f2be5875fe2ee1d7201fe2290020c344d9bde7":
        raise ValueError("Unexpected Viewer source revision")
    destination = ROOT / "engine/inspect_ai" / frontend
    # git archive represents the unexpanded gitlink as an empty directory.
    destination.rmdir()
    export(frontend_source, destination)
    project = ROOT / "engine/inspect_ai/pyproject.toml"
    content = project.read_text("utf-8")
    content = re.sub(r"\[tool.setuptools_scm\].*?(?=\n\[)", "", content, flags=re.S)
    content = content.replace('dynamic = ["version"]', 'version = "0.3.287+evals.1"')
    content = content.replace(
        'include = ["inspect_ai*"]',
        'include = ["inspect_ai*"]\n'
        'exclude = ["inspect_ai._view.ts-mono", "inspect_ai._view.ts-mono.*"]',
    )
    project.write_text(content, encoding="utf-8")
    # Hatch VCS also needs an explicit version outside its original Git tree.
    project = ROOT / "components/inspect_scout/pyproject.toml"
    content = project.read_text("utf-8")
    content = content.replace('dynamic = ["version"]', 'version = "0.5.5+evals.1"')
    content = re.sub(r"\[tool.hatch.version\].*?(?=\n\[)", "", content, flags=re.S)
    content = re.sub(r"\[tool.hatch.build.hooks.vcs\].*?(?=\n\[)", "", content, flags=re.S)
    content = re.sub(
        r"(?m)^inspect-ai = \{ git = .*?$",
        'inspect-ai = { path = "../../engine/inspect_ai", editable = true }',
        content,
    )
    project.write_text(content, encoding="utf-8")
    print("Imported fixed upstream source trees and expanded Viewer source")


if __name__ == "__main__":
    main()
