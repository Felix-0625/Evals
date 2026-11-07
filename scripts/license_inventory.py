"""Collect installed dependency notices without storing machine-specific paths."""

import argparse
import json
from importlib.metadata import distributions
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("frontend", type=Path, help="JSON from pnpm licenses list --json")
    parser.add_argument("--output", type=Path, default=Path("docs/licenses"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    frontend = json.loads(args.frontend.read_text("utf-8-sig"))
    entries, notices = [], []
    for license_name, packages in frontend.items():
        for package in packages:
            entries.append(
                {
                    "ecosystem": "npm",
                    "name": package["name"],
                    "versions": package["versions"],
                    "license": license_name,
                    "source": package.get("homepage", ""),
                }
            )
            for location in package["paths"]:
                for path in sorted(Path(location).iterdir()):
                    if path.is_file() and path.name.lower().startswith(
                        ("license", "licence", "notice", "copying")
                    ):
                        notices.append(
                            (
                                f"npm {package['name']} {package['versions']} / {path.name}",
                                path.read_text("utf-8", errors="replace"),
                            )
                        )
    for distribution in distributions():
        metadata = distribution.metadata
        name = metadata["Name"]
        if not name:
            continue
        license_name = metadata.get("License-Expression") or metadata.get("License")
        if name == "evals-app":
            license_name = "MIT"
        if name == "agent-client-protocol" and not license_name:
            # The pinned wheel includes Apache-2.0 text but omits metadata classifiers.
            license_name = "Apache-2.0 (see bundled LICENSE)"
        classifiers = [
            value.split(" :: ")[-1]
            for value in metadata.get_all("Classifier", [])
            if value.startswith("License ::")
        ]
        entries.append(
            {
                "ecosystem": "pypi",
                "name": name,
                "versions": [distribution.version],
                "license": license_name or "; ".join(classifiers) or "See bundled notice",
                "source": metadata.get("Home-page", ""),
            }
        )
        for item in distribution.files or []:
            if ".dist-info/" in str(item).replace("\\", "/") and item.name.lower().startswith(
                ("license", "licence", "notice", "copying")
            ):
                path = Path(distribution.locate_file(item))
                if path.is_file():
                    notices.append(
                        (
                            f"pypi {name} {distribution.version} / {item.name}",
                            path.read_text("utf-8", errors="replace"),
                        )
                    )
    entries.sort(key=lambda item: (item["ecosystem"], item["name"]))
    (args.output / "inventory.json").write_text(
        json.dumps(entries, indent=2, ensure_ascii=False) + "\n", "utf-8"
    )
    seen = set()
    blocks = []
    for title, content in notices:
        key = (title, content)
        if key not in seen:
            blocks.append(f"{'=' * 72}\n{title}\n{'=' * 72}\n{content}\n")
            seen.add(key)
    (args.output / "THIRD_PARTY_LICENSES.txt").write_text("\n".join(blocks), "utf-8")
    print(f"Inventoried {len(entries)} packages and {len(blocks)} notice files.")


if __name__ == "__main__":
    main()
