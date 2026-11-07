"""VeraAgent's real chat harness with deliberately failing and repeated tools."""

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from serve_local import make_observation, vera_runner


def actions(payload):
    repair = {
        "tool_name": "edit",
        "path": "calc.py",
        "edits": [{"oldText": "a-b-1", "newText": "a+b"}],
    }
    verify = {"command": "python -m unittest -q"}
    scenario = payload["metadata"]["scenario"]
    if scenario == "tool_failure":
        return [verify, repair, verify]
    if scenario == "duplicate_edit":
        return [repair, repair, verify]
    if scenario == "changed_data":
        return [repair, verify]
    raise ValueError(f"Unknown Vera fault scenario: {scenario}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    source = args.source.resolve()
    directory = args.artifacts.resolve() / uuid4().hex
    directory.mkdir(parents=True)
    os.chdir(directory)
    sandbox_parent = directory / "sandboxes"
    sandbox_parent.mkdir()
    tempfile.tempdir = str(sandbox_parent)
    payload = json.loads(sys.stdin.buffer.read())
    code = Path(__file__).read_bytes() + Path(__file__).with_name("serve_local.py").read_bytes()
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    version = commit + ":faults-" + hashlib.sha256(code).hexdigest()[:16]
    if payload["application_version"] != version:
        raise ValueError("Vera source or isolation adapter changed after freezing")
    with contextlib.redirect_stdout(sys.stderr):
        observation = vera_runner(source, version, directory, action_builder=actions)(payload)
    calls = observation["facts"]["process"]["calls"]
    scenario = payload["metadata"]["scenario"]
    tests = [call for call in calls if call["name"] == "bash"]
    edits = [call for call in calls if call["name"] == "edit"]
    if scenario == "tool_failure":
        verified = (
            len(tests) == 2
            and "FAILED (failures=1)" in str(tests[0]["result"])
            and "OK" in str(tests[1]["result"])
        )
    elif scenario == "duplicate_edit":
        verified = (
            len(edits) == 2
            and edits[0]["arguments"] == edits[1]["arguments"]
            and bool(edits[1]["error"])
        )
    else:
        verified = len(edits) == 1 and len(tests) == 1 and "OK" in str(tests[0]["result"])
    success = observation["facts"]["result"]["success"] and verified
    facts = {
        **observation["facts"],
        "recovery": {"consistent": success},
        "result": {"success": success},
        "fault_verification": {
            "scenario": scenario,
            "native_passed": observation["facts"]["result"]["success"],
            "tool_evidence_complete": verified,
        },
    }
    artifact = directory / "verified.json"
    artifact.write_text(json.dumps(facts, ensure_ascii=False, indent=2), "utf-8")
    print(
        json.dumps(
            make_observation(version, facts, artifact, observation["output"]), ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
