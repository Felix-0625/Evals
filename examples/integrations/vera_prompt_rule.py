"""Probe VeraAgent's rendered profile and in-turn verification in a disposable sandbox."""

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

COMMAND = "python -m unittest -q"
RULE_START = "Preserve the provided tests."
UNIT_DIVISOR = 1_000_000


def message_text(row):
    content = row["content"]
    if isinstance(content, str):
        return content
    return "\n".join(block["text"] for block in content if block["type"] == "text")


def scripted_model(observations):
    def factory(_payload):
        turn = 0

        def respond(messages):
            nonlocal turn
            system = "\n".join(message_text(row) for row in messages if row["role"] == "system")
            task = "\n".join(message_text(row) for row in messages if row["role"] == "user")
            rules = [
                line.removeprefix("- ")
                for line in system.splitlines()
                if line.startswith(f"- {RULE_START}")
            ]
            if len(rules) != 1:
                raise ValueError("Expected one rendered verification profile rule")
            rule = rules[0]
            observations.append(
                {
                    "turn": turn,
                    "rendered_rule": rule,
                    "system_sha256": hashlib.sha256(system.encode()).hexdigest(),
                    "user_names_command": COMMAND in task,
                    "rule_names_command": COMMAND in rule,
                    "profile_bytes": len(rule.encode("utf-8")),
                }
            )
            actions = []
            if turn == 0:
                actions = [
                    {
                        "tool_name": "edit",
                        "path": "calc.py",
                        "edits": [{"oldText": "a-b-1", "newText": "a+b"}],
                    }
                ]
            elif turn == 1 and (COMMAND in task or COMMAND in rule):
                actions = [{"command": COMMAND}]
            turn += 1
            return {
                "role": "assistant",
                "content": "Executing the requested step." if actions else "Finished.",
                "extra": {"actions": actions},
            }

        return respond

    return factory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    directory = args.artifacts.resolve() / uuid4().hex
    directory.mkdir(parents=True)
    os.chdir(directory)
    sandbox_parent = directory / "sandboxes"
    sandbox_parent.mkdir()
    tempfile.tempdir = str(sandbox_parent)
    payload = json.loads(sys.stdin.buffer.read())
    helper = Path(__file__)
    code = helper.read_bytes() + helper.with_name("serve_local.py").read_bytes()
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    version = commit + ":prompt-" + hashlib.sha256(code).hexdigest()[:16]
    if payload["application_version"] != version:
        raise ValueError("Vera source or prompt adapter changed after freezing")
    observations = []
    with contextlib.redirect_stdout(sys.stderr):
        observed = vera_runner(
            source, version, directory, response_factory=scripted_model(observations)
        )(payload)
    facts = observed["facts"]
    calls = facts["process"]["calls"]
    edits = [call for call in calls if call["name"] == "edit"]
    checks = [call for call in calls if call["name"] == "bash"]
    tests_preserved = len(edits) == 1 and edits[0]["arguments"].get("path") == "calc.py"
    verified = (
        len(checks) == 1
        and checks[0]["arguments"].get("command") == COMMAND
        and not checks[0]["error"]
        and "OK" in str(checks[0]["result"]).splitlines()
        and [call["name"] for call in calls] == ["edit", "bash"]
    )
    profile_bytes = sum(row["profile_bytes"] for row in observations)
    facts["process"]["acceptance_before_finish"] = verified
    facts["security"]["authorized"] &= tests_preserved
    facts["cost"]["known"] = bool(observations)
    facts["prompt_probe"] = {
        "model_calls": observations,
        "profile_bytes": profile_bytes,
        "tests_preserved": tests_preserved,
    }
    artifact = directory / "prompt-proof.json"
    artifact.write_text(json.dumps(facts, ensure_ascii=False, indent=2), "utf-8")
    observation = make_observation(version, facts, artifact, observed["output"], observed["error"])
    observation.update(
        cost=profile_bytes / UNIT_DIVISOR if observations else None,
        cost_source="scripted fixture tariff per million rendered profile bytes",
        pricing_version="profile-bytes-v1",
    )
    print(json.dumps(observation, ensure_ascii=False))


if __name__ == "__main__":
    main()
