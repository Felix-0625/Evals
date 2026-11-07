"""Synchronize built viewers and attach the notices required by their assets."""

import argparse
import shutil
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scout", action="store_true", help="Copy a freshly built Scout viewer")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    frontend = root / "engine/inspect_ai/src/inspect_ai/_view/ts-mono"
    target = root / "components/inspect_scout/src/inspect_scout/_view/dist"
    if args.scout:
        source = frontend / "apps/scout/dist"
        if not (source / "index.html").is_file():
            raise ValueError("Build the Scout viewer before copying release assets")
        # Both resolved paths are fixed descendants of this checkout.
        if not target.resolve().is_relative_to(root):
            raise ValueError("Viewer target escaped the repository")
        shutil.rmtree(target)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("*.map"))
    for viewer in [root / "engine/inspect_ai/src/inspect_ai/_view/dist", target]:
        notices = viewer / "licenses"
        notices.mkdir(exist_ok=True)
        shutil.copy2(root / "THIRD_PARTY_NOTICES.md", notices / "NOTICE.md")
        shutil.copy2(
            root / "docs/licenses/THIRD_PARTY_LICENSES.txt", notices / "THIRD_PARTY_LICENSES.txt"
        )
    print("Viewer notices synchronized.")


if __name__ == "__main__":
    main()
